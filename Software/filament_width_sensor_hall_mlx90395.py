import logging
from threading import Lock
from . import bus

MLX90395_DEFAULT_CHIP_ADDR = 0x0C

class FilamentWidthSensorHallMLX90395:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.name = config.get_name().split()[1]
        self.printer.add_object("filament_width_sensor_" + self.name, self)

        self.i2c = bus.MCU_I2C_from_config(config, default_addr=MLX90395_DEFAULT_CHIP_ADDR, default_speed=100000)
        self.mcu = self.i2c.get_mcu()
        self.i2cLock = Lock()

        self.dia1=config.getfloat('Cal_dia1', 1.5)
        self.dia2=config.getfloat('Cal_dia2', 2.0)
        self.rawdia1=config.getint('Raw_dia1', 10000)
        self.rawdia2=config.getint('Raw_dia2', -10000)

        self.gcode = self.printer.lookup_object('gcode')
        self.gcode.register_mux_command("MLX_QUERY_RAW", "SENSOR", self.name,
                                   self.cmd_get_raw_reading)
        
        self.printer.register_event_handler("klippy:connect", self._handle_connect)
    def _handle_connect(self):
        self._init_sensor()

    def _init_sensor(self):
        self._execute_command(0xF0) # Soft reset
        self.reactor.pause(self.reactor.monotonic() + .01)
        self._execute_command(0x80) # Exit mode (may not be nessecary)
        self.reactor.pause(self.reactor.monotonic() + .01)
        logging.info("MLX90395 with ID:" + self._read_register(0x26, 6).hex() + " found") # Read chip ID
        self._write_register_bits(0x01, 15, 1, 0x00) # Disable the int pin output incase it is shorted to gnd
        self._write_register_bits(0x00, 4, 4, 0x00) # Set gain
        self._write_register_bits(0x02, 9, 2, 0x00) # Set res
        self._write_register_bits(0x02, 0, 2, 0x03) # Set osr
        self._write_register_bits(0x02, 2, 3, 0x07) # Set averaging
        self._execute_command(0x10 | 0x08) # Start burst mode on the Z axis 

    def _read_diameter(self):
        raw = self._read_raw_val()
        return round((self.dia2 - self.dia1) / (self.rawdia2 - self.rawdia1) * (raw - self.rawdia1) + self.dia1 , 3)

    def _read_raw_val(self):
        answer = self._read_register(0x80 >> 1, 12)
        while(answer is None or answer[0] & 0x01 < 1): # Retry if data isn't fresh
            self.reactor.pause(self.reactor.monotonic() + .1)
            answer = self._read_register(0x80 >> 1, 12)
        value = int.from_bytes([answer[6], answer[7]], "big", signed=True)
        self.lastFilamentWidthReading = value
        return value

    def _read_register(self, reg, read_len):
        if self.i2cLock.acquire(True, 1):
            try:
                params = self.i2c.i2c_read([reg << 1], read_len)
                self.i2cLock.release()
                return bytearray(params['response'])
            finally:
                if self.i2cLock.locked():
                    self.i2cLock.release()

    def _write_register(self, reg, data):
        if type(data) is not list:
            data = [data]
        data.insert(0, reg << 1)
        self.i2c.i2c_write(data)

    def _write_register_bits(self, reg, offset, length, data):
        register_value = int.from_bytes(self._read_register(reg, 2), "big") # read whole register
        #logging.info(f"{register_value:16b}")
        mask = (pow(2, length)-1 << offset)
        register_value = register_value & (0xFFFF ^ mask) # zero the requested range
        register_value = register_value | ((data << offset) & mask) # insert data at requested range
        #logging.info(f"{register_value:16b}")
        self._write_register(reg, list(register_value.to_bytes(2, 'big'))) # write
        new_register_value = int.from_bytes(self._read_register(reg, 2), "big") # read whole register
        if register_value != new_register_value:
            logging.error(f"Setting {offset}:{length} in {reg} to {data} failed, expected {register_value:16b} got {new_register_value:16b}")
    
    def _execute_command(self, command):
        if type(command) is not list:
            command = [command]
        command.insert(0, 0x80)
        self.i2c.i2c_write(command)

    def get_reading(self, position):
        return self._read_diameter()
    
    def cmd_get_raw_reading(self, gcmd):
        accumulator = 0.0
        averaging = gcmd.get_int('AVERAGING', 1, 1)
        for x in range(averaging):            
            accumulator += self._read_raw_val()
        value = int(accumulator / averaging)
        gcmd.respond_info(f"{self.name}: {value} (averaged over {averaging} readings)")
    
def load_config_prefix(config):
    return FilamentWidthSensorHallMLX90395(config)