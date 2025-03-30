from . import filament_switch_sensor
import logging

class FilamentWidthCompensation:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.runout_helper = filament_switch_sensor.RunoutHelper(config)
        self.gcode = self.printer.lookup_object('gcode')

        self.sensor = self.printer.lookup_object("filament_width_sensor_" + config.get("sensor"))
        self.poll_time = config.getfloat('poll_time', 1)
        self.is_active = config.getboolean('compensation_enabled', False)
        self.runout_active = config.getboolean('runout_enabled', False)
        self.oversize_active = config.getboolean('oversize_enabled', False)

        self.MEASUREMENT_INTERVAL_MM=config.getint('measurement_interval',10)
        self.nominal_filament_dia = config.getfloat('default_nominal_filament_diameter', above=1)
        self.use_current_dia_while_delay = config.getboolean('use_current_dia_while_delay', False)
        self.measurement_delay = config.getfloat('measurement_delay', above=0.)
        self.runout_delay = config.getfloat('runout_delay', 0)
        self.measurement_max_difference = config.getfloat('max_difference', 0.2)
        self.max_diameter = (self.nominal_filament_dia + self.measurement_max_difference)
        self.min_diameter = (self.nominal_filament_dia - self.measurement_max_difference)

        self.runout_dia = config.getfloat('runout_diameter', 1.0)
        self.oversize_dia = config.getfloat('oversize_diameter', 1.9)
        self.is_log = config.getboolean('logging', False)

        self.filament_array = []
        self.array_reached = False
        self.runoutOccured = False
        self.runoutPosition = 0

        # printer objects
        self.toolhead = self.ppins = None
        self.printer.register_event_handler("klippy:ready", self.handle_ready)

        self.update_timer = self.reactor.register_timer(self.update_event)

        self.gcode = self.printer.lookup_object('gcode')
        self.gcode.register_command('QUERY_FILAMENT_WIDTH', self.cmd_query_filament_width)
        self.gcode.register_command('DUMP_FILAMENT_ARRAY', self.cmd_dump_filament_array)
        self.gcode.register_command('CLEAR_FILAMENT_ARRAY', self.cmd_clear_filament_array)
        self.gcode.register_command('ENABLE_FILAMENT_WIDTH_COMPENSATION', self.cmd_enable_filament_width_compensation)
        self.gcode.register_command('DISABLE_FILAMENT_WIDTH_COMPENSATION', self.cmd_disable_filament_width_compensation)
        self.gcode.register_command('ENABLE_FILAMENT_WIDTH_RUNOUT', self.cmd_enable_filament_width_runout)
        self.gcode.register_command('DISABLE_FILAMENT_WIDTH_RUNOUT', self.cmd_disable_filament_width_runout)
        self.gcode.register_command('ENABLE_FILAMENT_WIDTH_OVERSIZE', self.cmd_enable_filament_width_oversize)
        self.gcode.register_command('DISABLE_FILAMENT_WIDTH_OVERSIZE', self.cmd_disable_filament_width_oversize)
        self.gcode.register_command('ENABLE_FILAMENT_WIDTH_LOGGING', self.cmd_enable_filament_width_logging)
        self.gcode.register_command('DISABLE_FILAMENT_WIDTH_LOGGING', self.cmd_disable_filament_width_logging)
        self.gcode.register_command('SET_FILAMENT_WIDTH_OVERSIZE_DIAMETER', self.cmd_set_filament_width_oversize_diameter)
        self.gcode.register_command('SET_FILAMENT_WIDTH_RUNOUT_DIAMETER', self.cmd_disable_filament_runout_diameter)

    def handle_ready(self):
        # Load printer objects
        self.toolhead = self.printer.lookup_object('toolhead')
        self.motion_report = self.printer.lookup_object("motion_report")
        self.gcode_move = self.printer.lookup_object("gcode_move")

        # Start extrude factor update timer
        self.reactor.update_timer(self.update_timer, self.reactor.NOW)


    def update_filament_array(self, dia, last_epos):
        # Fill array
        if len(self.filament_array) > 0:
            # Get last reading position in array & calculate next
            # reading position
          next_reading_position = (self.filament_array[-1][0] +
          self.MEASUREMENT_INTERVAL_MM)
          if next_reading_position <= (last_epos + self.measurement_delay):
            self.filament_array.append([last_epos + self.measurement_delay, dia])
            if self.is_log:
                self.log_line(dia, last_epos)
        else:
            # add first item to array
            self.filament_array.append([self.measurement_delay
                                        + last_epos, dia])
            self.firstExtruderUpdatePosition = (self.measurement_delay
                                                + last_epos)
            if self.is_log:
                self.log_line(dia, last_epos)

    def update_event(self, eventtime):
        current_epos = self.motion_report.get_status(eventtime)['live_position'].e

        reading = self.sensor.get_reading(current_epos)
        self.update_filament_array(reading, current_epos)

        runout = (not self.runout_active or reading > self.runout_dia) and (not self.oversize_active or reading < self.oversize_dia)

        if self.runout_delay <= 0:
            self.runout_helper.note_filament_present(eventtime, is_filament_present=runout)
        else:
            if runout:
                self.runoutOccured = True
                self.runoutPosition = current_epos + self.measurement_delay
            if self.runoutOccured and current_epos + self.runout_delay >= self.runoutPosition:
                self.runout_helper.note_filament_present(eventtime, is_filament_present=False)
                self.runoutOccured = False
            else:
                self.runout_helper.note_filament_present(eventtime, is_filament_present=True)

        while len(self.filament_array) > 0 and self.filament_array[0][0] <= current_epos:
            self.filament_array.pop(0)
            self.array_reached = True

        if self.is_active:
            diameter = self.nominal_filament_dia
            position = -1
            if len(self.filament_array) > 0:
                array_entry = self.filament_array[0]
                if self.use_current_dia_while_delay or self.array_reached:
                    diameter = array_entry[1]
                    position = array_entry[0]

            if diameter > self.min_diameter and diameter < self.max_diameter:
              percentage = round(self.nominal_filament_dia**2 / diameter**2 * 100, 3)
            else:
              percentage = 100.0
            self.gcode.run_script("M221 S" + str(percentage) + "POSITION" + str(position))

        if self.is_active or self.runout_active or self.oversize_active:
            return eventtime + self.poll_time
        else:
            return self.reactor.NEVER

    def log_line(self, dia, last_epos):
        self.gcode.respond_info(f"Filament width:{dia}, {last_epos + self.measurement_delay}")

    def cmd_query_filament_width(self, gcmd):
        gcmd.respond_info(f"{self.sensor.get_reading(0)}")

    def cmd_dump_filament_array(self, gcmd):
        gcmd.respond_info(f"filament_width_compensation_array, {len(self.filament_array)}, {self.filament_array}")

    def cmd_clear_filament_array(self, gcmd):
        self.filament_array.clear()
        self.array_reached = False

    def cmd_enable_filament_width_compensation(self, gcmd):
        self.is_active = True
        self.reactor.update_timer(self.update_timer, self.reactor.NOW)

    def cmd_disable_filament_width_compensation(self, gcmd):
        self.is_active = False

    def cmd_enable_filament_width_runout(self, gcmd):
        self.runout_active = True
        self.reactor.update_timer(self.update_timer, self.reactor.NOW)

    def cmd_disable_filament_width_runout(self, gcmd):
        self.runout_active = False

    def cmd_enable_filament_width_oversize(self, gcmd):
        self.oversize_active = True
        self.reactor.update_timer(self.update_timer, self.reactor.NOW)

    def cmd_disable_filament_width_oversize(self, gcmd):
        self.oversize_active = False

    def cmd_enable_filament_width_logging(self, gcmd):
        self.is_log = True
        self.reactor.update_timer(self.update_timer, self.reactor.NOW)

    def cmd_disable_filament_width_logging(self, gcmd):
        self.is_log = False

    def cmd_set_filament_width_oversize_diameter(self, gcmd):
        self.oversize_dia = gcmd.getfloat('OVERSIZE_DIAMETER', self.oversize_dia)

    def cmd_disable_filament_runout_diameter(self, gcmd):
        self.runout_dia = gcmd.getfloat('RUNOUT_DIAMETER', self.runout_dia)

def load_config(config):
    return FilamentWidthCompensation(config)
