"""
Vantage Controller module for interacting with the infusion controller via TCP.
Basic operations for enumerating and controlling the loads are supported.

Author: Greg J. Badros

Originally based on pylutron which was written by Dima Zavin

See also https://www.npmjs.com/package/vantage-infusion
and https://github.com/angeloxx/homebridge-vantage/blob/master/index.js

To use with home assistant and its virtual python environment, you need to:

$ cd .../path/to/home-assistant/
$ pip3 install --upgrade .../path/to/pyvantage

Then the component/vantage.py and its require line will work.

"""

__Author__ = "Greg J. Badros"
__copyright__ = "Copyright 2018, 2019 Greg J. Badros"

# TODO:
# Handle OmniSensor elements:
#     <Model>Temperature</Model> using getsensor (for temperature in celsius)
#     <Model>Power</Model> using getpower (for power in watts)
#     <Model>Current</Model> using getcurrent (for current in amps)


import logging
import telnetlib
import socket
import threading
import time
import base64
import re
import json
from colormath.color_objects import sRGBColor, HSVColor
from colormath.color_conversions import convert_color
from collections import deque


def xml_escape(s):
    """Escape XML meta characters '<' and '&'."""
    answer = s.replace("<", "&lt;")
    answer = answer.replace("&", "&amp;")
    return answer


def kelvin_to_level(kelvin):
    """Convert kelvin temperature to a USAI level."""
    if kelvin < 2200:
        return 0
    if kelvin > 6000:
        return 100.0
    return (kelvin-2200)/(6000-2200) * 100


def level_to_kelvin(level):
    """Convert a level to a kelvin temperature."""
    if level < 0:
        return 2200
    if level > 100:
        return 6000
    return (6000-2200) * level/100 + 2200


def level_to_mireds(level):
    """Convert a level to mired color temperature."""
    kelvin = level_to_kelvin(level)
    mireds = 1000000/kelvin
    return mireds


_LOGGER = logging.getLogger(__name__)


class VantageException(Exception):
    """Top level module exception."""
    pass


class VIDExistsError(VantageException):
    """Asserted when registerering a duplicate integration id."""
    pass


class ConnectionExistsError(VantageException):
    """Raised when a connection already exists (e.g. two connect() calls)."""
    pass


class VantageConnection(threading.Thread):
    """Encapsulates the connection to the Vantage controller."""

    def __init__(self, host, user, password, cmd_port, recv_callback):
        """Initializes the vantage connection, doesn't actually connect."""
        threading.Thread.__init__(self)

        self._host = host
        self._user = user
        self._password = password
        self._cmd_port = cmd_port
        self._telnet = None
        self._connected = False
        self._lock = threading.RLock()
        self._connect_cond = threading.Condition(lock=self._lock)
        self._recv_cb = recv_callback
        self._done = False

        self.setDaemon(True)

    def connect(self):
        """Connects to the vantage controller."""
        if self._connected or self.is_alive():
            raise ConnectionExistsError("Already connected")
        # After starting the thread we wait for it to post us
        # an event signifying that connection is established. This
        # ensures that the caller only resumes when we are fully connected.
        self.start()
        with self._lock:
            self._connect_cond.wait_for(lambda: self._connected)

    # VantageConnection
    def _send_ascii_nl_locked(self, cmd):
        """Sends the specified command to the vantage controller.
        Assumes lock is held."""
        _LOGGER.debug("Vantage send_ascii_nl: %s", cmd)
        try:
            self._telnet.write(cmd.encode('ascii') + b'\r\n')
        except BrokenPipeError:
            _LOGGER.warning("Vantage BrokenPipeError - disconnected")
            raise

    def send_ascii_nl(self, cmd):
        """Sends the specified command to the lutron controller.

        Must not hold self._lock"""
        with self._lock:
            self._send_ascii_nl_locked(cmd)

    def _do_login_locked(self):
        """Executes the login procedure (telnet) as well as setting up some
        connection defaults like turning off the prompt, etc."""
        while True:
            try:
                self._telnet = telnetlib.Telnet(self._host, self._cmd_port)
                break
            except Exception as e:
                _LOGGER.warning("Could not connect to %s:%d, "
                                "retrying after 3 sec (%s)",
                                self._host, self._cmd_port,
                                e)
                time.sleep(3)
                continue
        self._send_ascii_nl_locked("LOGIN " + self._user + " " + self._password)
        self._telnet.read_until(b'\r\n')
        self._send_ascii_nl_locked("STATUS LOAD")
        self._telnet.read_until(b'\r\n')
        self._send_ascii_nl_locked("STATUS BLIND")
        self._telnet.read_until(b'\r\n')
        self._send_ascii_nl_locked("STATUS BTN")
        self._telnet.read_until(b'\r\n')
        self._send_ascii_nl_locked("STATUS VARIABLE")
        return True

    def _disconnect_locked(self):
        self._connected = False
        self._connect_cond.notify_all()
        self._telnet = None
        _LOGGER.warning("Disconnected")

    def _maybe_reconnect(self):
        """Reconnects to controller if we have been previously disconnected."""
        with self._lock:
            if not self._connected:
                _LOGGER.info("Connecting to %s", self._host)
                self._do_login_locked()
                self._connected = True
                self._connect_cond.notify_all()
                _LOGGER.info("Connected")

    def run(self):
        """Main thread to maintain connection and receive remote status."""
        _LOGGER.debug("VantageConnection run started")
        while True:
            self._maybe_reconnect()
            try:
                line = self._telnet.read_until(b"\n")
            except EOFError:
                _LOGGER.warning("run got EOFError")
                with self._lock:
                    self._disconnect_locked()
                continue
            except BrokenPipeError:
                _LOGGER.warning("run got BrokenPipeError")
                with self._lock:
                    self._disconnect_locked()
                continue
            self._recv_cb(line.decode('ascii').rstrip())


def _desc_from_t1t2(t1, t2):
    if not t2:
        desc = t1 or ''
    else:
        desc = t1 + ' ' + t2
    return desc.strip()


class VantageXmlDbParser():
    """The parser for Vantage XML database.

    The database describes all the rooms (Area), keypads (Device), and switches
    (Output). We handle the most relevant features, but some things like LEDs,
    etc. are not implemented."""

    def __init__(self, vantage, xml_db_str):
        """Initializes the XML parser from raw XML data as string input."""
        self._vantage = vantage
        self._xml_db_str = xml_db_str
        self.outputs = []
        self.variables = []
        self.tasks = []
        self.buttons = []
        self.keypads = []
        self.sensors = []
        self.load_groups = []
        self.vid_to_area = {}
        self.vid_to_load = {}
        self.vid_to_keypad = {}
        self.vid_to_button = {}
        self.vid_to_variable = {}
        self.vid_to_task = {}
        self.vid_to_sensor = {}
        self.name_to_task = {}
        self.vid_to_shade = {}
        self._name_area_to_vid = {}
        self._vid_to_colorvid = {}
        self.project_name = None

    def parse(self):
        """Main entrypoint into the parser.

        It interprets and creates all the relevant Vantage objects and
        stuffs them into the appropriate hierarchy.
        """

        import xml.etree.ElementTree as ET

        root = ET.fromstring(self._xml_db_str)
        # The structure of a Lutron config is something like this:
        # <Areas>
        #   <Area ...>
        #     <DeviceGroups ...>
        #     <Scenes ...>
        #     <ShadeGroups ...>
        #     <Outputs ...>
        #     <Areas ...>
        #       <Area ...>
        # Vantage uses a flatter style with elements that are:
        # Area (with @VID and <Name> and <Area> (parent VID) )
        # Load (with @VID and <Name> and <Area> (enclosing Area VID))
        # GMem (with @VID and <Name> [variables])
        # Task (with @VID and <Name> )
        # OmniSensor (with @VID and <Name> )
        # Timer (with @VID and <Name> )
        # Keypad (with @VID and <Name> )

        areas = root.findall(".//Objects//Area[@VID]")
        for area_xml in areas:
            if self.project_name is None:
                self.project_name = area_xml.find('Name').text
                _LOGGER.debug("Set project name to %s", self.project_name)
            area = self._parse_area(area_xml)
            _LOGGER.debug("Area = %s", area)
            self.vid_to_area[area.vid] = area

        loads = root.findall(".//Objects//Load[@VID]")
        loads = loads + root.findall(".//Objects//Vantage.DDGColorLoad[@VID]")
        for load_xml in loads:
            output = self._parse_output(load_xml)
            if output is None:
                continue
            self.outputs.append(output)
            self.vid_to_load[output.vid] = output
            _LOGGER.debug("Output = %s", output)
            self.vid_to_area[output.area].add_output(output)

        load_groups = root.findall(".//Objects//LoadGroup[@VID]")
        for lg_xml in load_groups:
            lgroup = self._parse_load_group(lg_xml)
            if lgroup is None:
                continue
            self.load_groups.append(lgroup)
            self.outputs.append(lgroup)
            self.vid_to_load[lgroup.vid] = lgroup
            _LOGGER.debug("load group = %s", lgroup)
            self.vid_to_area[lgroup.area].add_output(lgroup)

        keypads = root.findall(".//Objects//Keypad[@VID]")
        keypads = keypads + root.findall(".//Objects//DualRelayStation[@VID]")
        for kp_xml in keypads:
            keypad = self._parse_keypad(kp_xml)
            _LOGGER.debug("keypad = %s", keypad)
            self.vid_to_keypad[keypad.vid] = keypad
            self.vid_to_area[keypad.area].add_keypad(keypad)
            self.keypads.append(keypad)

        buttons = root.findall(".//Objects//Button[@VID]")
        for button_xml in buttons:
            b = self._parse_button(button_xml)
            if not b:
                continue
            _LOGGER.debug("b = %s", b)
            self.vid_to_button[b.vid] = b
            if b.area != -1:
                self.vid_to_area[b.area].add_button(b)
                self.buttons.append(b)

        drycontacts = root.findall(".//Objects//DryContact[@VID]")
        for dc_xml in drycontacts:
            dc = self._parse_drycontact(dc_xml)
            if not dc:
                continue
            _LOGGER.debug("dc = %s", dc)
            self.vid_to_button[dc.vid] = dc
            self.buttons.append(dc)

        variables = root.findall(".//Objects//GMem[@VID]")
        for v in variables:
            var = self._parse_variable(v)
            _LOGGER.debug("var = %s", var)
            self.vid_to_variable[var.vid] = var
            # N.B. variables have categories, not areas, so no add to area
            self.variables.append(var)

        omnisensors = root.findall(".//Objects//OmniSensor[@VID]")
        for s in omnisensors:
            sensor = self._parse_omnisensor(s)
            _LOGGER.debug("sensor = %s", sensor)
            self.vid_to_sensor[sensor.vid] = sensor
            # N.B. variables have categories, not areas, so no add to area
            self.sensors.append(sensor)

        lightsensors = root.findall(".//Objects//LightSensor[@VID]")
        for s in lightsensors:
            sensor = self._parse_lightsensor(s)
            _LOGGER.debug("sensor = %s", sensor)
            self.vid_to_sensor[sensor.vid] = sensor
            # N.B. variables have categories, not areas, so no add to area
            self.sensors.append(sensor)

        tasks = root.findall(".//Objects//Task[@VID]")
        for t in tasks:
            task = self._parse_task(t)
            _LOGGER.debug("task = %s", task)
            self.vid_to_task[task.vid] = task
            self.name_to_task[task.name] = task
            # N.B. tasks have categories, not areas, so no add to area
            self.tasks.append(task)

        # Lots of different shade types, one xpath for each kind of shade
        # MechoShade driver shades
        shades = \
            root.findall(".//Objects//MechoShade.IQ2_Shade_Node_CHILD[@VID]")
        shades = (shades +
                  root.findall(".//Objects//MechoShade.IQ2_Group_CHILD[@VID]"))
        # Native QIS QMotion shades
        shades = shades + root.findall(".//Objects//QISBlind[@VID]")
        shades = shades + root.findall(".//Objects//BlindGroup[@VID]")
        # Non-native QIS Driver QMotion shades (the old way)
        shades = (shades +
                  root.findall(".//Objects//QMotion.QIS_Channel_CHILD[@VID]"))
        # Somfy radio-controlled
        shades = (shades +
                  root.findall(".//Objects//Somfy.URTSI_2_Shade_CHILD[@VID]"))
        # Somfy RS-485 SDN wired shades
        shades = (shades +
                  root.findall(".//Objects//Somfy.RS-485_Shade_CHILD[@VID]"))

        for shade_xml in shades:
            shade = self._parse_shade(shade_xml)
            if shade is None:
                continue
            self.vid_to_shade[shade.vid] = shade
            self.outputs.append(shade)
            _LOGGER.debug("shade = %s", shade)

        _LOGGER.debug("self._name_area_to_vid = %s", self._name_area_to_vid)

        return True

    def _parse_area(self, area_xml):
        """Parses an Area tag, which is effectively a room, depending on how the
        Vantage controller programming was done."""
        area = Area(self._vantage,
                    name=area_xml.find('Name').text,
                    parent=int(area_xml.find('Area').text),
                    vid=int(area_xml.get('VID')),
                    note=area_xml.find('Note').text)
        return area

    def _parse_variable(self, var_xml):
        """Parses a variable (GMem) tag."""
        var = Variable(self._vantage,
                       name=var_xml.find('Name').text,
                       vid=int(var_xml.get('VID')))
        return var

    def _parse_omnisensor(self, sensor_xml):
        """Parses an OmniSensor tag."""
        kind = sensor_xml.find('Model').text.lower()
        var = OmniSensor(self._vantage,
                         name=sensor_xml.find('Name').text,
                         kind=kind,
                         vid=int(sensor_xml.get('VID')))
        return var

    def _parse_lightsensor(self, sensor_xml):
        """Parses a LightSensor object."""
        value_range = (float(sensor_xml.find('RangeLow').text),
                       float(sensor_xml.find('RangeHigh').text))
        return LightSensor(self._vantage,
                           name=sensor_xml.find('Name').text,
                           area=int(sensor_xml.find('Area').text),
                           value_range=value_range,
                           vid=int(sensor_xml.get('VID')))

    def _parse_shade(self, shade_xml):
        """Parses a sahde node.

        Either a MechoShade.IQ2_Shade_Node_CHILD or
        QMotion.QIS_Channel_CHILD (shade) tag.
        """
        shade = Shade(self._vantage,
                      name=shade_xml.find('Name').text,
                      area_vid=int(shade_xml.find('Area').text),
                      vid=int(shade_xml.get('VID')))
        return shade

    def _parse_output(self, output_xml):
        """Parses a load, which is generally a switch controlling a set of
        lights/outlets, etc."""
        out_name = output_xml.find('DName').text
        if out_name:
            out_name = out_name.strip()
        if not out_name or out_name.isspace():
            out_name = output_xml.find('Name').text.strip()
        area_vid = int(output_xml.find('Area').text)

        area_name = self.vid_to_area[area_vid].name.strip()
        lt_xml = output_xml.find('LoadType')
        if lt_xml is not None:
            load_type = lt_xml.text.strip()
        else:
            load_type = output_xml.find('ColorType').text.strip()

        output_type = 'LIGHT'
        vid = int(output_xml.get('VID'))

        # TODO: find a better heuristic so that on/off lights still show up
        if load_type == 'High Voltage Relay':  # 'Low Voltage Relay'
            output_type = 'RELAY'

        if ' COLOR' in out_name and load_type != 'HID':
            _LOGGER.warning("Load %s [%d] might be color load "
                            "but of type %s not HID",
                            out_name, vid, load_type)

        if load_type == 'HID':
            output_type = 'COLOR'
            omit_trailing_color_re = re.compile(r'\s+COLOR\s*$')
            load_name = omit_trailing_color_re.sub("", out_name)
            _LOGGER.debug("Found HID Type, guessing load name is %s",
                          load_name)

            load_vid = self._name_area_to_vid.get((load_name, area_vid))
            if load_vid:
                self._vid_to_colorvid[load_vid] = vid
                _LOGGER.info("Found colorvid = %d for load_vid %d"
                             " (names %s and %s) in area %s (%d)",
                             vid, load_vid, out_name, load_name,
                             area_name, area_vid)
                self.vid_to_load[load_vid].color_control_vid = vid
            else:
                # TODO: do not assume that the regular loads are
                # handled before the COLOR loads
                _LOGGER.warning("Could not find matching load for "
                                "COLOR load %s (%d) in area %s (%d)",
                                out_name, vid, area_name, area_vid)

        # it's a DMX color load if and only if it's RGB or RGBW loadtype
        # and Channel2 is nonempty
        # (we represent dynamic white as a R+B (no green) RGB load,
        # and that only support_color_temp)
        dmx_color = False
        if load_type.startswith("RGB"):
            ch1 = output_xml.find('Channel1')
            ch2 = output_xml.find('Channel2')
            ch3 = output_xml.find('Channel3')
            # _LOGGER.debug("ch1 = %s, ch2 = %s", ch1.text, ch2.text)
            if not(ch1.text and ch1.text.strip() != ""):
                _LOGGER.warning("RGB* load with missing Channel1: %s",
                                out_name)
            if not(ch3.text and ch3.text.strip() != ""):
                _LOGGER.warning("RGB* load with missing Channel3: %s",
                                out_name)
            if load_type == "RGBW":
                if not(ch2.text and ch2.text.strip() != ""):
                    _LOGGER.warning("RGBW load with missing Channel2: %s",
                                    out_name)
                dmx_color = True
            else:   # load_type == "RGB"
                if ch2.text and ch2.text.strip() != "":
                    dmx_color = True
                else:
                    # just a dynamic white red/blue light
                    # (just two shades of white, really)
                    load_type = "DW"

        if output_type == 'LIGHT':
            self._name_area_to_vid[(out_name, area_vid)] = vid
        output = Output(self._vantage,
                        name=out_name,
                        area=area_vid,
                        output_type=output_type,
                        load_type=load_type,
                        cc_vid=(load_vid if output_type == 'COLOR'
                                else self._vid_to_colorvid.get(vid)),
                        dmx_color=dmx_color,
                        vid=vid)
        return output

    def _parse_load_group(self, output_xml):
        """Parses a load group, which is a set of loads"""
        out_name = output_xml.find('DName').text
        if out_name:
            out_name = out_name.strip()
        if not out_name or out_name.isspace():
            out_name = output_xml.find('Name').text
        else:
            _LOGGER.debug("Using dname = %s", out_name)
        area_vid = int(output_xml.find('Area').text)

#        area_name = self.vid_to_area[area_vid].name
        loads = output_xml.findall('./LoadTable/Load')
        vid = output_xml.get('VID')
        vid = int(vid)

        load_vids = []
        dmx_color = True
        for load in loads:
            v = int(load.text)
            load_vids.append(v)
            if not self.vid_to_load[v]._dmx_color:
                dmx_color = False
            else:
                _LOGGER.warning("for loadgroup %d, vid %s supports color",
                                vid, v)

        output = LoadGroup(self._vantage,
                           name=out_name,
                           area=area_vid,
                           load_vids=load_vids,
                           dmx_color=dmx_color,
                           vid=vid)
        return output

    def _parse_keypad(self, keypad_xml):
        """Parses a keypad device."""
        area_vid = int(keypad_xml.find('Area').text)
        keypad = Keypad(self._vantage,
                        name=keypad_xml.find('Name').text + ' [K]',
                        area=area_vid,
                        vid=int(keypad_xml.get('VID')))
        return keypad

    def _parse_task(self, task_xml):
        """Parses a task object."""
        task = Task(self._vantage,
                    name=task_xml.find('Name').text,
                    vid=int(task_xml.get('VID')))
        return task

    def _parse_drycontact(self, dc_xml):
        """Parses a button device that part of a keypad."""
        vid = int(dc_xml.get('VID'))
        name = dc_xml.find('Name').text + ' [C]'
        parent = dc_xml.find('Parent')
        parent_vid = int(parent.text)
        area = -1  # TODO could try to get area for this
        num = 0
        keypad = None
        _LOGGER.info("Found DryContact with vid = %d", vid)
        # Ugh, this is awful -- three different ways of representing bad-value
        button = Button(self._vantage, name, area, vid, num,
                        parent_vid, keypad, False)
        return button

    def _parse_button(self, button_xml):
        """Parses a button device that part of a keypad."""
        vid = int(button_xml.get('VID'))
        name = button_xml.find('Name').text + ' [B]'
        # no Text1 sub-element on DryContact
        parent = button_xml.find('Parent')
        parent_vid = int(parent.text)
        text1 = button_xml.find('Text1').text
        text2 = button_xml.find('Text2').text
        desc = _desc_from_t1t2(text1, text2)
        num = int(parent.get('Position'))
        keypad = self._vantage._ids['KEYPAD'].get(parent_vid)
        if keypad is None:
            _LOGGER.warning("No parent vid = %d for button vid = %d "
                            "(leaving button out)",
                            parent_vid, vid)
            return None
        area = keypad.area
        button = Button(self._vantage, name, area, vid, num, parent_vid,
                        keypad, desc)
        keypad.add_button(button)
        return button


# Connect to port 2001 and write
# "<IBackup><GetFile><call>Backup\\Project.dc</call></GetFile></IBackup>"
# to get a Base64 response of the last XML file of the designcenter config.
# Then use port 3001 to send commands.

# maybe need
# <ILogin><Login><call><User>USER</User><Password>PASS</Password></call></Login></ILogin>


class Vantage():
    """Main Vantage Controller class.

    This object owns the connection to the controller, the rooms that
    exist in the network, handles dispatch of incoming status updates,
    etc.

    """

    # See vantage host commands reference
    # (you may need to be a dealer/integrator for access)
    # Response lines come back from Vantage with this prefix
    OP_RESPONSE = 'R:'
    # Status report lines come back from Vantage with this prefix
    OP_STATUS = 'S:'

    def __init__(self, host, user, password,
                 only_areas=None, exclude_areas=None,
                 cmd_port=3001, file_port=2001,
                 name_mappings=None):
        """Initializes the Vantage object. No connection is made to the remote
        device."""
        self._host = host
        self._user = user
        self._password = password
        self._name = None
        self._conn = VantageConnection(host, user, password, cmd_port,
                                       self._recv)
        self._cmds = deque([])
        self._name_mappings = name_mappings
        self._file_port = file_port
        self._only_areas = only_areas
        self._exclude_areas = exclude_areas
        self._ids = {}
        self._names = {}   # maps from unique name to id
        self._subscribers = {}
        self._vid_to_area = {}  # copied out from the parser
        self._vid_to_load = {}  # copied out from the parser
        self._vid_to_variable = {}  # copied out from the parser
        self._vid_to_task = {}  # copied out from the parser
        self._vid_to_shade = {}  # copied out from the parser
        self._vid_to_sensor = {}  # copied out from the parser
        self._name_to_task = {}  # copied out from the parser
        self._r_cmds = ['LOGIN', 'LOAD', 'STATUS', 'GETLOAD', 'VARIABLE',
                        'ERROR',
                        'TASK', 'GETBLIND', 'BLIND', 'INVOKE',
                        'GETLIGHT', 'GETPOWER', 'GETCURRENT',
                        'GETSENSOR', 'ADDSTATUS', 'DELSTATUS',
                        'GETCUSTOM', 'RAMPLOAD', 'GETTEMPERATURE']
        self._s_cmds = ['LOAD', 'TASK', 'BTN', 'VARIABLE', 'BLIND', 'STATUS']
        self.outputs = None
        self.variables = None
        self.tasks = None
        self.buttons = None
        self.keypads = None
        self.sensors = None

    def subscribe(self, obj, handler):
        """Subscribes to status updates of the requested object.

        The handler will be invoked when the controller sends a
        notification regarding changed state. The user can then
        further query the object for the state itself.

        """

        self._subscribers[obj] = handler

    def get_lineage_from_obj(self, obj):
        """Return list of areas for obj, chasing up to top."""
        count = 0
        area = self._vid_to_area.get(obj.area)
        if area is None:
            return []
        answer = [area.name]
        while area and count < 10:
            count += 1
            parent_vid = area.parent
            if parent_vid == 0:
                break
            area = self._vid_to_area.get(parent_vid)
            if area:
                answer.append(area.name)
#    _LOGGER.debug("lineage for " + str(obj.vid) + " is " + str(answer))
        return answer

    # TODO: cleanup this awful logic
    def register_id(self, cmd_type, cmd_type2, obj):
        """Registers an object (through its vid [vantage id]).

        This lets it receive update notifications. This is the core
        mechanism how Output and Keypad objects get notified when the
        controller sends status updates.

        """

        ids = self._ids.setdefault(cmd_type, {})
        ids = self._ids.setdefault(cmd_type2, {})
        if obj.vid in ids:
            raise VIDExistsError("VID exists %s" % obj.vid)
        self._ids[cmd_type][obj.vid] = obj
        if cmd_type2:
            self._ids[cmd_type2][obj.vid] = obj
        lineage = self.get_lineage_from_obj(obj)
        name = ""
        # reverse all but the last element in list
        for n in reversed(lineage[:-1]):
            ns = n.strip()
            if ns.startswith('Station Load '):
                continue
            if ns.startswith('Color Load '):
                continue
            if self._name_mappings:
                mapped_name = self._name_mappings.get(ns.lower())
                if mapped_name is not None:
                    if mapped_name is True:
                        continue
                    ns = mapped_name
            name += ns + "-"

        # TODO: this may be a little too hacky
        # it makes sure that we use "GH-Bedroom High East"
        # instead of "GH-GH Bedroom High East"
        # since it's sometimes convenient to have the short area
        # at the start of the device name in vantage
        if obj.name.startswith(name[0:-1]):
            obj.name = name + obj.name[len(name):]
        else:
            obj.name = name + obj.name

        if obj.name in self._names:
            oldname = obj.name
            obj.name += " (%s)" % (str(obj.vid))
            if '0-10V RELAYS' in oldname or cmd_type == 'BTN':
                _LOGGER.info("Repeated name `%s' - adding vid to get %s",
                             oldname, obj.name)
            else:
                _LOGGER.warning("Repeated name `%s' - adding vid to get %s",
                                oldname, obj.name)
        self._names[obj.name] = obj.vid

    # TODO: update this to handle async status updates
    def _recv(self, line):
        """Invoked by the connection manager to process incoming data."""
        _LOGGER.debug("_recv got line: %s", line)
        if line == '':
            return
        typ = None
        # Only handle query response messages, which are also sent on remote
        # status updates (e.g. user manually pressed a keypad button)
        if line[0] == 'R':
            cmds = self._r_cmds
            typ = 'R'
            if len(self._cmds) > 0:
                this_cmd = self._cmds.popleft()
            else:
                this_cmd = "__UNDERFLOW__"
        elif line[0] == 'S':
            cmds = self._s_cmds
            typ = 'S'
        else:
            _LOGGER.error("_recv got unknown line start character")
            return
        parts = re.split(r'[ :]', line[2:])
        cmd_type = parts[0]
        vid = parts[1]
        args = parts[2:]
        if cmd_type not in cmds:
            _LOGGER.warning("Unknown cmd %s (%s)", cmd_type, line)
            return
        if cmd_type == 'LOGIN':
            _LOGGER.info("login successful")
            return
        # TODO: is it okay to ignore R:RAMPLOAD responses?
        # or do we need to handle_update_and_notify like with "LOAD",
        # below
        if line[0] == 'R' and cmd_type in ('STATUS', 'ADDSTATUS',
                                           'DELSTATUS', 'INVOKE',
                                           'GETCUSTOM', 'RAMPLOAD',
                                           'GETTEMPERATURE'):
            return
        if line[0] == 'R' and cmd_type == "ERROR":
            _LOGGER.warning("Vantage %s on command: %s", line,
                            this_cmd)
            return
        # is there ever an S:ERROR line? that's all the below covers
        if cmd_type == 'ERROR':
            _LOGGER.error("_recv got ERROR line: %s", line)
            return
        if cmd_type in ('GETLOAD', 'GETPOWER', 'GETCURRENT',
                        'GETSENSOR', 'GETLIGHT'):
            cmd_type = cmd_type[3:]  # strip "GET" from front
        elif cmd_type == 'GETBLIND':
            return
        elif cmd_type == 'TASK':
            return
        elif cmd_type == 'VARIABLE':
            _LOGGER.debug("vantage variable set response: %s", line)

        ids = self._ids.get(cmd_type)
        if ids is None:
            _LOGGER.warning("Might need to handle cmd_type ids: %s:: %s",
                            cmd_type, line)
        else:
            if not vid.isdigit():
                _LOGGER.warning("VID %s is not an integer", vid)
                return
            vid = int(vid)
            if vid not in ids:
                _LOGGER.warning("Unknown id %d (%s)", vid, line)
                return
            obj = ids[vid]
            # First let the device update itself
            if (typ == 'S' or
                    (typ == 'R' and
                     cmd_type in ('LOAD', 'POWER', 'CURRENT',
                                  'SENSOR', 'LIGHT'))):
                self.handle_update_and_notify(obj, args)

    def handle_update_and_notify(self, obj, args):
        """Call handle_update for the obj and for subscribers."""
        handled = obj.handle_update(args)
        # Now notify anyone who cares that device may have changed
        if handled and handled in self._subscribers:
            self._subscribers[handled](handled)

    def connect(self):
        """Connects to the Vantage controller.

        The TCP connection is used both to send commands and to
        receive status responses.

        """
        self._conn.connect()

    # Vantage
    def send_cmd(self, cmd):
        """Send the host command to the Vantage TCP socket."""
        self._cmds.append(cmd)
        self._conn.send_ascii_nl(cmd)

    # Vantage
    def send(self, op, vid, *args):
        """Formats and sends the command to the controller."""
#    out_cmd = ",".join(
#        (cmd, str(vid)) + tuple((str(x) for x in args)))
        out_cmd = str(vid) + " " + " ".join(str(a) for a in args)
        self.send_cmd(op + " " + out_cmd)

    # TODO: could confirm that this variable exists in the XML we download
    # and/or lookup the variables VID so that we can set it by name
    def set_variable_vid(self, vid, value):
        """Sets variable with vid to value;
        be sure instance type of value is either int or string"""
        num = re.compile(r'^\d+$')
        if isinstance(value, int) or num.match(value):
            self.send_cmd("VARIABLE " + str(vid) + " " + str(value))
        else:
            p = re.compile(r'["\n\r]')
            if p.match(value):
                raise Exception("Newlines and quotes are "
                                "not allowed in Text values")
            self.send_cmd("VARIABLE " + str(vid) +
                          ' "' + value + '"')

    def call_task_vid(self, vid):
        """Call the task with vid."""
        num = re.compile(r'^\d+$')
        if isinstance(vid, int) or num.match(vid):
            task = self._vid_to_task.get(int(vid))
            if task is None:
                _LOGGER.warning("Vid %d is not registered as a task", vid)
            # call it regardless
            self.send_cmd("TASK " + str(vid) + " RELEASE")
            _LOGGER.info("Calling task %s", task)
        else:
            _LOGGER.warning("Could not interpret %d as task vid", vid)

    def call_task(self, name):
        """Call the task with name NAME.
        This is fragile - consider using call_task_vid.

        """
        task = self._name_to_task.get(name)
        if task is not None:
            self.send_cmd("TASK " + str(task.vid) + " RELEASE")
            _LOGGER.info("Calling task %s", task)
        else:
            _LOGGER.warning("No task with name = %s", name)

    def load_xml_db(self, disable_cache=False):
        """Load the Vantage database from the server."""
        filename = self._host + "_config.txt"
        xml_db = ""
        success = False
        if not disable_cache:
            try:
                f = open(filename, "r")
                xml_db = f.read()
                f.close()
                success = True
                _LOGGER.info("read cached vantage configuration file %s",
                             filename)
            except Exception as e:
                _LOGGER.warning("Failed loading cached config: %s",
                                e)
        if not success:
            _LOGGER.info("doing request for vantage configuration file")
            if disable_cache:
                _LOGGER.info("Vantage config cache is disabled.")
            ts = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            ts.connect((self._host, self._file_port))
            if self._user:
                ts.send(("<ILogin><Login><call><User>%s</User>"
                         "<Password>%s</Password>"
                         "</call></Login></ILogin>\n"
                         % (xml_escape(self._user),
                            xml_escape(self._password))).encode("ascii"))
                response = ""
                while not response.endswith("</ILogin>\n"):
                    response += ts.recv(4096).decode('ascii')
                check_return_true = re.compile(r'<return>(.*?)</return>')
                m = check_return_true.search(response)
                if m is None:
                    raise Exception(
                        "Could not find response code from controller "
                        "upon login attempt, response = "  + response)
                if m.group(1) != "true":
                    raise Exception("Login failed, return code is: " +
                                    m.group(1))
            _LOGGER.info("sent GetFile request")
            ts.send("<IBackup><GetFile><call>Backup\\Project.dc"
                    "</call></GetFile></IBackup>\n".encode("ascii"))
            ts.settimeout(1)
            try:
                response = bytearray()
                while True:
                    dbytes = ts.recv(2**20)
                    if not dbytes:
                        break
                    response.extend(dbytes)
            except EOFError:
                ts.close()
                _LOGGER.error("Failed to read vantage configuration file -"
                              " check username and password")
                exit(-1)
            except socket.timeout:
                ts.close()
            _LOGGER.debug("done reading")
            response = response.decode('ascii')
            response = response[response.find("</Result>\n")+10:]
            response = response.replace('<?File Encode="Base64" /', '')
            response = response.replace('?>', '')
            response = response[:response.find('</return>')]
            dbytes = base64.b64decode(response)
            xml_db = dbytes.decode('utf-8')
            try:
                f = open(filename, "w")
                f.write(xml_db)
                f.close()
                _LOGGER.info("wrote file %s", filename)
            except Exception as e:
                _LOGGER.warning("could not save %s (%s)",
                                filename, e)

        _LOGGER.info("Loaded xml db")
        # print(xml_db[0:10000])

        parser = VantageXmlDbParser(vantage=self, xml_db_str=xml_db)
        self._vid_to_load = parser.vid_to_load
        self._vid_to_variable = parser.vid_to_variable
        self._vid_to_area = parser.vid_to_area
        self._vid_to_shade = parser.vid_to_shade
        self._name = parser.project_name

        parser.parse()
        self.outputs = parser.outputs
        self.variables = parser.variables
        self.tasks = parser.tasks
        self.buttons = parser.buttons
        self.keypads = parser.keypads
        self.sensors = parser.sensors
        self._vid_to_load = parser.vid_to_load
        self._vid_to_variable = parser.vid_to_variable
        self._vid_to_area = parser.vid_to_area
        self._vid_to_shade = parser.vid_to_shade
        self._vid_to_task = parser.vid_to_task
        self._vid_to_sensor = parser.vid_to_sensor
        self._name_to_task = parser.name_to_task
        self._name = parser.project_name

        _LOGGER.info("Found Vantage project: %s, %d areas, %d loads, "
                     "%d variables, and %d shades",
                     self._name,
                     len(self._vid_to_area.keys()),
                     len(self._vid_to_load.keys()),
                     len(self._vid_to_variable.keys()),
                     len(self._vid_to_shade.keys()))

        return True


class _RequestHelper():
    """A class to help with sending queries to the controller and waiting for
    responses.

    It is a wrapper used to help with executing a user action
    and then waiting for an event when that action completes.

    The user calls request() and gets back a threading.Event on which they then
    wait.

    If multiple clients of a vantage object (eg an Output) want to get a status
    update on the current brightness (output level), we don't want to spam the
    controller with (near)identical requests. So, if a request is pending, we
    just enqueue another waiter on the pending request and return a new Event
    object. All waiters will be woken up when the reply is received and the
    wait list is cleared.

    NOTE: Only the first enqueued action is executed as the assumption is that
    the queries will be identical in nature.
    """

    def __init__(self):
        """Initialize the request helper class."""
        self.__lock = threading.Lock()
        self.__events = []

    def request(self, action):
        """Request an action to be performed, in case one."""
        ev = threading.Event()
        first = False
        with self.__lock:
            if not self.__events:
                first = True
            self.__events.append(ev)
        if first:
            action()
        return ev

    def notify(self):
        """Have all events pending trigger, and reset to []."""
        with self.__lock:
            events = self.__events
            self.__events = []
        for ev in events:
            ev.set()


class VantageEntity:
    """Base class for all the Vantage objects we'd like to manage. Just holds basic
    common info we'd rather not manage repeatedly."""

    def __init__(self, vantage, name, area, vid):
        """Initializes the base class with common, basic data."""
        assert name is not None
        self._vantage = vantage
        self._name = name
        self._area = area
        self._vid = vid
        self._extra_info = {}

    def needs_poll(self):
        return False

    @property
    def name(self):
        """Returns the entity name (e.g. Pendant)."""
        return self._name

    @name.setter
    def name(self, value):
        """Sets the entity name to value."""
        self._name = value

    @property
    def vid(self):
        """The integration id"""
        return self._vid

    @property
    def id(self):
        """The integration id"""
        return self._vid

    @property
    def area(self):
        """The area vid"""
        return self._area

    @property
    def full_lineage(self):
        """Return list of areas for self."""
        areas = []
        avid = self._area
        c = 0
        while True and c < 5:
            c += 1
            area = self._vantage._vid_to_area.get(avid)
            if area is None:
                break
            areas.append(area.name)
            avid = area.parent
            if avid == 0:
                break
        areas = areas[::-1]
        areas.append(self._name)
        return areas

    def handle_update(self, _):
        """The handle_update callback is invoked when an event is received
        for the this entity.

        Returns:
            self - If event was valid and was handled.
            None - otherwise.
        """
        return None

    @property
    def kind(self):
        """The type of object (for units in hass)."""
        return None

    @property
    def extra_info(self):
        """Map of extra info."""
        return self._extra_info

    def is_output(self):
        """Return true iff this is an output."""
        return False


class Area():
    """An area (i.e. a room) that contains devices/outputs/etc."""
    def __init__(self, vantage, name, parent, vid, note):
        self._vantage = vantage
        self._name = name
        self._vid = vid
        self._note = note
        self._parent = parent
        self._outputs = []
        self._keypads = []
        self._buttons = []
        self._sensors = []
        self._variables = []
        self._tasks = []

    def __str__(self):
        """Returns a pretty-printed string for this object."""
        return 'Area name: "%s", vid: %d, parent_vid: %d' % (
            self._name, self._vid, self._parent)

    def add_output(self, output):
        """Adds an output object that's part of this area, only used during
        initial parsing."""
        self._outputs.append(output)

    def add_keypad(self, keypad):
        """Adds a keypad object that's part of this area, only used during
        initial parsing."""
        self._keypads.append(keypad)

    def add_button(self, button):
        """Adds a button object that's part of this area, only used during
        initial parsing."""
        self._buttons.append(button)

    def add_sensor(self, sensor):
        """Adds a motion sensor object that's part of this area, only used during
        initial parsing."""
        self._sensors.append(sensor)

    def add_variable(self, v):
        """Adds a variable object that's part of this area, only used during
        initial parsing."""
        self._variables.append(v)

    def add_task(self, t):
        """Adds a task object that's part of this area, only used during
        initial parsing."""
        self._tasks.append(t)

    @property
    def name(self):
        """Returns the name of this area."""
        return self._name

    @property
    def parent(self):
        """Returns the vid of the parent area."""
        return self._parent

    @property
    def vid(self):
        """The integration id of the area."""
        return self._vid

    @property
    def outputs(self):
        """Return the tuple of the Outputs from this area."""
        return tuple(output for output in self._outputs)

    @property
    def keypads(self):
        """Return the tuple of the Keypads from this area."""
        return tuple(keypad for keypad in self._keypads)

    @property
    def sensors(self):
        """Return the tuple of the MotionSensors from this area."""
        return tuple(sensor for sensor in self._sensors)


class Variable(VantageEntity):
    """A variable in the vantage system. See set_variable_vid.

    """
    CMD_TYPE = 'VARIABLE'  # GMem in the XML config

    def __init__(self, vantage, name, vid):
        """Initializes the variable object."""
        super(Variable, self).__init__(vantage, name, None, vid)
        self._value = None
        self._vantage.register_id(Variable.CMD_TYPE, None, self)

    def __str__(self):
        """Returns pretty-printed representation of this object."""
        return 'Variable name: "%s", vid: %d, value: %s' % (
            self._name, self._vid, self._value)

    @property
    def value(self):
        """The value of the variable."""
        return self._value

    @property
    def kind(self):
        """The type of object (for units in hass)."""
        return 'variable'

    def handle_update(self, args):
        """Callback invoked by the main event loop.

        This handles a new value for the variable.

        """
        value = float(args[0])
        _LOGGER.debug("Setting variable %s (%d) to %s",
                      self._name, self._vid, value)
        self._value = value
        return self


class Output(VantageEntity):
    """This is the output entity in Vantage universe. This generally refers to a
    switched/dimmed load, e.g. light fixture, outlet, etc."""
    CMD_TYPE = 'LOAD'
    ACTION_ZONE_LEVEL = 1
    _wait_seconds = 0.03  # TODO:move this to a parameter

    def __init__(self, vantage, name, area, output_type, load_type,
                 cc_vid, dmx_color, vid):
        """Initializes the Output."""
        super(Output, self).__init__(vantage, name, area, vid)
        self._output_type = output_type
        self._load_type = load_type
        self._level = 0.0
        self._color_temp = 2700
        self._rgb = [0, 0, 0]
        self._hs = [0, 0]
        # if _load_type == 'COLOR' then _color_control_vid
        # is the load's vid,
        # else it's the color control vid
        self._color_control_vid = cc_vid
        self._dmx_color = dmx_color
        self._query_waiters = _RequestHelper()
        self._ramp_sec = [0, 0, 0]  # up, down, color
        self._vantage.register_id(Output.CMD_TYPE,
                                  "STATUS" if dmx_color else None,
                                  self)
        self._addedstatus = False

    def __str__(self):
        """Returns a pretty-printed string for this object."""
        return (
            "Output name: '%s' area: %d type: '%s' load: '%s'"
            "vid: %d %s%s%s [%s]" % (
                self._name, self._area, self._output_type,
                self._load_type, self._vid,
                ("(dim) " if self.is_dimmable else ""),
                ("(ctemp) " if selfelf.support_color_temp else ""),
                ("(color) " if self.support_color else ""),
                self.full_lineage))

    def __repr__(self):
        """Returns a stringified representation of this object."""
        return str({'name': self._name, 'vid': self._vid, 'area': self._area,
                    'type': self._load_type, 'load': self._load_type,
                    'supports':
                    ("ctemp " if self.support_color_temp else "") +
                    ("color " if self.support_color else "")})

    @property
    def simple_name(self):
        """Return a simple pretty-printed string for this object."""
        return 'VID:%d (%s) [%s]' % (self._vid, self._name, self._load_type)

    # ADDSTATUS
    # DELSTATUS
    # S:STATUS [vid] RGBLoad.GetRGB [val] [ch[012]]
    # S:STATUS [vid] RGBLoad.GetRGBW [val] [ch[0123]]
    # S:STATUS [vid] RGBLoad.GetHSL [val] [ch[012]]
    # S:STATUS [vid] RGBLoad.GetColor [value]
    # S:STATUS [vid] RGBLoad.GetColorName [value]
    # INVOKE [vid] RGBLoad.SetRGBW [val0], [val1], [val2], [val3]
    def handle_update(self, args):
        """Handles an event update for this object.
        E.g. dimmer level change

        """
        _LOGGER.debug("vantage - handle_update %d -- %s", self._vid, args)
        if len(args) == 1:
            level = float(args[0])
            if self._output_type == 'COLOR':
                color_temp = level_to_kelvin(level)
                light = self._vantage._vid_to_load.get(self._color_control_vid)
                if light:
                    light._color_temp = color_temp
                    _LOGGER.debug("Received color change of VID %d "
                                  "set load VID %d to color = %d",
                                  self._vid, self._color_control_vid,
                                  color_temp)
                    light._query_waiters.notify()
                    return light
                _LOGGER.warning("Received color change of VID %d but cannot "
                                "find corresponding load", self._vid)
                return self
            _LOGGER.debug("Updating brightness %d(%s): l=%f",
                          self._vid, self._name, level)
            self._level = level
            self._query_waiters.notify()
        else:
            if args[0] == 'RGBLoad.GetRGB':
                _LOGGER.warning("RGBLoad.GetRGB, handling vid = %d; "
                                "RGBW %s %s",
                                self._vid, args[1], args[2])
                val = int(args[1])
                char = int(args[2])
                if char < 3:
                    self._rgb[char] = val
                if char == 2:
                    self._query_waiters.notify()
        return self

    def __do_query_level(self):
        """Helper to perform the actual query the current dimmer level of the
        output. For pure on/off loads the result is either 0.0 or 100.0."""
        if self.support_color and not self._addedstatus:
            self._vantage.send("ADDSTATUS", self._vid)
            self._addedstatus = True
        _LOGGER.debug("getload of %s", self._vid)
        self._vantage.send("GETLOAD", self._vid)

    def last_level(self):
        """Returns last cached value of output level, no query is performed."""
        return self._level

    @property
    def support_color_temp(self):
        """Returns true iff this load can be set to a color temperature."""
        return ((self._color_control_vid is not None) or
                self._load_type == "DW" or
                self._load_type.startswith('RGB'))

    @property
    def support_color(self):
        """Returns true iff this load is full-color."""
        return self._dmx_color

    @property
    def level(self):
        """Returns the current output level by querying the controller."""
        ev = self._query_waiters.request(self.__do_query_level)
        ev.wait(self._wait_seconds)
        return self._level

    @level.setter
    def level(self, new_level):
        """Sets the new output level."""
        if self._level == new_level:
            return

        if new_level == 0:
            ramp_sec = self._ramp_sec[1]
        else:
            ramp_sec = self._ramp_sec[0]
        self._vantage.send("RAMPLOAD", self._vid, new_level, ramp_sec)
        self._level = new_level

    @property
    def rgb(self):
        """Returns current color of the light."""
        return self._rgb

    @rgb.setter
    def rgb(self, new_rgb):
        """Sets new color for the light."""
        if self._rgb == new_rgb:
            return
        # we need to adjust the rgb values to take into account the level
        r = self._level/100
        _LOGGER.debug("rgb = %s", json.dumps(new_rgb))
        # INVOKE [vid] RGBLoad.SetRGBW [val0], [val1], [val2], [val3]
        self._vantage.send("INVOKE", self._vid,
                           ("RGBLoad.SetRGBW %d %d %d %d" %
                            (new_rgb[0]*r, new_rgb[1]*r, new_rgb[2]*r, 0)))
        srgb = sRGBColor(*new_rgb)
        hs_color = convert_color(srgb, HSVColor)
        self._hs = [hs_color.hsv_h, hs_color.hsv_s]
        self._rgb = new_rgb

    @property
    # hue is scaled 0-360, saturation is 0-100
    def hs(self):
        """Returns current HS of the light."""
        return self._hs

    @hs.setter
    def hs(self, new_hs):
        """Sets new Hue/Saturation levels."""
        if self._hs == new_hs:
            return
        _LOGGER.debug("hs = %s", json.dumps(new_hs))
        hs_color = HSVColor(new_hs[0], new_hs[1], 1.0)
        rgb = convert_color(hs_color, sRGBColor)
        self._vantage.send("INVOKE", self._vid,
                           "RGBLoad.SetRGBW %d %d %d %d" %
                           (rgb.rgb_r, rgb.rgb_g, rgb.rgb_b, 0))
        self._rgb = [rgb.rgb_r, rgb.rgb_g, rgb.rgb_b]
        self._hs = new_hs

    @property
    def color_temp(self):
        """Returns the current output level by querying the controller."""
        # TODO: query the color temp
#    ev = self._query_waiters.request(self.__do_query_color)
#    ev.wait(self._wait_seconds)
        return self._color_temp

    @color_temp.setter
    def color_temp(self, new_color_temp):
        """Sets the new color temp level."""
        if self._color_temp == new_color_temp:
            return
        if self._dmx_color or self._load_type == "DW":
            _LOGGER.debug("Ignoring call to setter for color_temp "
                         "of dmx_color light %d",
                         self._vid)
        else:
            self._vantage.send("RAMPLOAD", self._color_control_vid,
                               kelvin_to_level(new_color_temp),
                               self._ramp_sec[2])
        self._color_temp = new_color_temp

# At some later date, we may want to also specify fade and delay times
#  def set_level(self, new_level, fade_time, delay):
#    self._vantage.send(Vantage.OP_EXECUTE, Output.CMD_TYPE,
#        Output.ACTION_ZONE_LEVEL, new_level, fade_time, delay)

    @property
    def color_control_vid(self):
        """Returns the color control vid, if any, for this light."""
        return self._color_control_vid

    @color_control_vid.setter
    def color_control_vid(self, new_ccvid):
        """Sets the color control vid for this light."""
        self._color_control_vid = new_ccvid

    @property
    def kind(self):
        """Returns the output type. At present AUTO_DETECT or NON_DIM."""
        return self._output_type

    @property
    def is_dimmable(self):
        """Returns a boolean of whether or not the output is dimmable."""
        return self._load_type.lower().find("non-dim") == -1

    def set_ramp_sec(self, up, down, color):
        """Set the ramp speed for load changes, in seconds."""
        self._ramp_sec = [up, down, color]

    def get_ramp_sec(self):
        """Return the current ramp speed settings."""
        return self._ramp_sec

    def is_output(self):
        return True


class Button(VantageEntity):
    """This object represents a keypad button that we can trigger and handle
    events for (button presses)."""

    CMD_TYPE = 'BTN'  # for a button

    def __init__(self, vantage, name, area, vid, num, parent, keypad, desc):
        super(Button, self).__init__(vantage, name, area, vid)
        self._num = num
        self._parent = parent
        self._keypad = keypad
        self._desc = desc
        self._value = None  # the last action reported
        self._vantage.register_id(Button.CMD_TYPE, None, self)

    def __str__(self):
        """Pretty printed string value of the Button object."""
        return 'Button name: "%s" num: %d area: %s vid: %d parent: %d [%s]' % (
            self._name, self._num, self._area, self._vid,
            self._parent, self._desc)

    def __repr__(self):
        """String representation of the Button object."""
        return str({'name': self._name, 'num': self._num,
                    'area': self._area, 'vid': self._vid,
                    'desc': self._desc})

    @property
    def value(self):
        """The value of the last action of the button."""
        return self._value

    @property
    def kind(self):
        """The type of object (for units in hass)."""
        if self._desc is False:
            return 'contact'
        return 'button'

    @property
    def number(self):
        """Returns the button number."""
        return self._num

    def handle_update(self, args):
        """The callback invoked by the main event loop.

        This handles an event from this keypad.

        """
        action = args[0]
        _LOGGER.debug("Button %d(%s): action=%s params=%s",
                      self._vid, self._name, action, args[1:])
        if self._keypad:  # it's a button
            self._value = action
            # this transfers control to Keypad.handle_update(...)
            self._vantage.handle_update_and_notify(
                self._keypad, [self._num, self._name, self._value])
        else:  # it's a drycontact
            # TODO: support per-vid flipping/control of these rewrites
            if action == 'PRESS':
                self._value = 'Violated'
            elif action == 'RELEASE':
                self._value = 'Normal'
            else:
                _LOGGER.warning(
                    "unexpected action for drycontact button %s = %s",
                    self, action)
                self._value = action

        return self


class LoadGroup(Output):
    """Represent a Vantage LoadGroup."""
    def __init__(self, vantage, name, area, load_vids, dmx_color, vid):
        """Initialize a load group"""
        super(LoadGroup, self).__init__(
            vantage, name, area, 'GROUP', 'GROUP', None, dmx_color, vid)
        self._load_vids = load_vids

    def __str__(self):
        """Returns a pretty-printed string for this object."""
        return ("Output name: '%s' area: %d type: '%s' load: '%s'"
                "id: %d %s%s%s (%s) [%s]" % (
                    self._name, self._area, self._output_type,
                    self._load_type, self._vid,
                    ("(dim) " if self.is_dimmable else ""),
                    ("(ctemp) " if self.support_color_temp else ""),
                    ("(color) " if self.support_color else ""),
                    self._load_vids,
                    self.full_lineage))


class Keypad(VantageEntity):
    """Object representing a Vantage keypad.

    Currently we don't really do much with it except handle the events
    (and drop them on the floor).
    """
    CMD_TYPE = 'KEYPAD'  # for a keypad

    def __init__(self, vantage, name, area, vid):
        """Initializes the Keypad object."""
        super(Keypad, self).__init__(vantage, name, area, vid)
        self._buttons = []
        self._value = None
        self._vantage.register_id(Keypad.CMD_TYPE, None, self)

    def add_button(self, button):
        """Adds a button that's part of this keypad. We'll use this to
        dispatch button events."""
        self._buttons.append(button)

    def __str__(self):
        """Returns a pretty-printed string for this object."""
        return 'Keypad name: "%s", area: "%s", vid: %d' % (
            self._name, self._area, self._vid)

    @property
    def buttons(self):
        """Return a tuple of buttons for this keypad."""
        return tuple(button for button in self._buttons)

    @property
    def kind(self):
        """The type of object (for units in hass)."""
        return 'keypad'

    @property
    def value(self):
        """The value of the variable."""
        return self._value

    def handle_update(self, args):
        """The callback invoked by a button's handle_update to
        set keypad value to the name of button."""
        _LOGGER.debug("Keypad %d(%s): %s",
                      self._vid, self._name, args)
        self._value = args[0]
        ei = {}
        ei['button_name'] = args[1]
        ei['button_action'] = args[2]
        self._extra_info = ei
        return self


class Task(VantageEntity):
    """Object representing a Vantage task.

    """
    CMD_TYPE = 'TASK'

    def __init__(self, vantage, name, vid):
        """Initializes the Task object."""
        super(Task, self).__init__(vantage, name, 0, vid)
        self._vantage.register_id(Task.CMD_TYPE, None, self)

    def __str__(self):
        """Returns a pretty-printed string for this object."""
        return 'Task name: "%s", vid: %d' % (
            self._name, self._vid)

    def handle_update(self, args):
        """Handle events from the task object.

        This callback is invoked by the main event loop.

        """
        component = int(args[0])
        action = int(args[1])
        params = [int(x) for x in args[2:]]
        _LOGGER.debug("Task %d(%s): c=%d a=%d params=%s",
                      self._vid, self._name, component, action, params)
        return self


class PollingSensor(VantageEntity):
    """Base class for LightSensor and OmniSensor.
    These sensors do not report values via STATUS commands
    but instead need to be polled."""

    def __init__(self, vantage, name, area, vid):
        """Init base fields"""
        assert name is not None
        super(PollingSensor, self).__init__(vantage, name, area, vid)
        self._value = None
        self._kind = None

    def needs_poll(self):
        return True

    @property
    def value(self):
        """The value of the variable."""
        return self._value

    @property
    def kind(self):
        """The type of object (for units in hass)."""
        return self._kind

    def update(self):
        """Request an update from the device."""
        self._vantage.send("GET"+self._kind.upper(), self._vid)

    def handle_update(self, args):
        """Handle sensor updates.

        This callback invoked by the main event loop.

        """
        value = float(args[0])
        _LOGGER.debug("Setting sensor (%s) %s (%d) to %s",
                      self._name, self._kind, self._vid, value)
        self._value = value
        return self


class LightSensor(PollingSensor):
    """Represent LightSensor devices."""
    CMD_TYPE = 'LIGHT'

    def __init__(self, vantage, name, area, value_range, vid):
        """Initializes the motion sensor object."""
        assert name is not None
        super(LightSensor, self).__init__(vantage, name, area, vid)
        self._kind = 'light'
        self.value_range = value_range
        self._vantage.register_id(self.CMD_TYPE, None, self)

    def __str__(self):
        """Returns pretty-printed representation of this object."""
        return 'LightSensor name (%s): "%s", vid: %d, value: %s' % (
            self._name, self._kind, self._vid, self._value)


class OmniSensor(PollingSensor):
    """An omnisensor in the vantage system."""
    CMD_TYPE = 'SENSOR'  # OmniSensor in the XML config

    def __init__(self, vantage, name, kind, vid):
        """Initializes the sensor object."""
        super(OmniSensor, self).__init__(vantage, name, None, vid)
        self._kind = kind
        self._vantage.register_id(self._kind.upper(), None, self)

    def __str__(self):
        """Returns pretty-printed representation of this object."""
        return 'OmniSensor name (%s): "%s", vid: %d, value: %s' % (
            self._name, self._kind, self._vid, self._value)


class Shade(VantageEntity):
    """A shade in the vantage system.

    """
    CMD_TYPE = 'BLIND'  # MechoShade.IQ2_Shade_Node_CHILD in the XML config
    _wait_seconds = 0.03  # TODO:move this to a parameter

    def __init__(self, vantage, name, area_vid, vid):
        """Initializes the shade object."""
        super(Shade, self).__init__(vantage, name, area_vid, vid)
        self._level = 100
        self._load_type = 'BLIND'
        self._vantage.register_id(Shade.CMD_TYPE, None, self)
        self._query_waiters = _RequestHelper()

    @property
    def kind(self):
        """Returns the output type. At present AUTO_DETECT or NON_DIM."""
        return self._load_type

    def __str__(self):
        """Returns pretty-printed representation of this object."""
        return 'Shade name: "%s", vid: %d, area: %d, level: %s' % (
            self._name, self._vid, self._area, self._level)

    def __repr__(self):
        """Returns a stringified representation of this object."""
        return str({'name': self._name, 'area': self._area,
                    'type': self._load_type, 'vid': self._vid})

    def last_level(self):
        """Returns last cached value of the output level, no query is performed."""
        return self._level

    @property
    def level(self):
        """The level (i.e. position) of the shade.
        Returns the current output level by querying the remote controller."""
        ev = self._query_waiters.request(self.__do_query_level)
        ev.wait(self._wait_seconds)
        return self._level

    @level.setter
    def level(self, new_level):
        """Sets the new output level."""
        if self._level == new_level:
            return
        if new_level == 0:
            self.close()
        elif new_level == 100:
            self.open()
        else:
            if new_level is not None:
                self._vantage.send("BLIND", self._vid, "POS", str(new_level))
        self._level = new_level

    def __do_query_level(self):
        """Helper to fetch the current [possibly inferred] shade level
        as a percentage of open. 100 = fully open."""
        self._vantage.send("GETBLIND", self._vid)

    def open(self):
        """Open the shade."""
        self._vantage.send("BLIND", self._vid, "OPEN")

    def stop(self):
        """Stop the shade."""
        self._vantage.send("BLIND", self._vid, "STOP")

    def close(self):
        """Stop the shade."""
        self._vantage.send("BLIND", self._vid, "CLOSE")

    def handle_update(self, args):
        """Handle new value for shade.

        This callback is invoked by the main event loop.

        """
        value = args[0]
        if value == "OPEN":
            value = 100.0
        elif value == "CLOSE":
            value = 0.0
        elif value == "STOP":
            value = None
        elif value == "POS":
            value = float(args[1])
        else:
            value = float(value)
        _LOGGER.debug("Setting shade %s (%d) to float %s",
                      self._name, self._vid, str(value))
        self._level = value
        return self
