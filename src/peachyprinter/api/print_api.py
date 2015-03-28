import logging
logger = logging.getLogger('peachy')
import time
from os import path, listdir

from peachyprinter.infrastructure.file import FileWriter
from peachyprinter.infrastructure.path_to_points import PathToPoints
from peachyprinter.infrastructure.controller import Controller
from peachyprinter.infrastructure.timed_drip_zaxis import TimedDripZAxis, PhotoZAxis
from peachyprinter.infrastructure.zaxis import SerialDripZAxis
from peachyprinter.domain.laser_control import LaserControl
from peachyprinter.infrastructure.micro_disseminator import MicroDisseminator
from peachyprinter.infrastructure.communicator import SerialCommunicator, NullCommunicator
from peachyprinter.infrastructure.gcode_layer_generator import GCodeReader
from peachyprinter.infrastructure.transformer import HomogenousTransformer
from peachyprinter.infrastructure.layer_generators import SubLayerGenerator, ShuffleGenerator, OverLapGenerator
from peachyprinter.infrastructure.commander import SerialCommander, NullCommander
from peachyprinter.infrastructure.notification import EmailNotificationService, EmailGateway
from peachyprinter.infrastructure.layer_control import LayerWriter, LayerProcessing
from peachyprinter.infrastructure.machine import *


class PrintQueueAPI(object):
    def __init__(self, configuration, status_call_back=None):
        self._configuration = configuration
        self._files = []
        self._api = None
        self._status_call_back = status_call_back
        self._configuration.options.print_queue_delay

    def call_back(self, status):
        if self._status_call_back:
            self._status_call_back(status)
        if status['status'] == "Complete":
            if self._api:
                self._api.close()
            logger.info('Print Complete proceeding to next file')
            if len(self._files) > 0:
                logger.info('Waiting %s seconds before proceeding to next file' % self._configuration.options.print_queue_delay)
                time.sleep(self._configuration.options.print_queue_delay)
                logger.info('Proceeding to next file')
                self.print_next()
            else:
                logger.info('Print Queue Complete')

    def print_next(self):
        afile = self._files.pop(0)
        logger.info("Printing Next File: %s" % afile)
        self._api = PrintAPI(self._configuration, self.call_back)
        self._api.print_gcode(afile)

    def print_folder(self, folder):
        self._files = self._get_files(folder)
        self.print_next()

    def _get_files(self, folder):
        if not path.isdir(folder):
            logger.info('Folder Specified Does Not Exist')
            raise Exception('Folder Specified Does Not Exist')
        all_files = [path.join(folder, item) for item in listdir(folder) if item.endswith('.gcode')]
        if len(all_files) == 0:
            logger.info('Folder Contains No Valid Files')
            raise Exception('Folder Contains No Valid Files')
        return all_files

    def close(self):
        self._files = []
        if self._api:
            self._api.close()


'''API designed to use configuration to print a thing'''


class PrintAPI(object):
    def __init__(self, configuration, start_height=0.0, status_call_back=None):
        logger.info('Print API Startup')
        self._configuration = configuration
        logger.info('Printer Name: %s' % self._configuration.name)
        self._controller = None
        self._status_call_back = status_call_back
        self._zaxis = None
        self._start_height = start_height
        self._current_file_name = None
        self._current_file = None
        if self._configuration.email.on:
            self._email_gateway = EmailGateway(self._configuration.email.host, self._configuration.email.port, self._configuration.email.username, self._configuration.email.password)
            self._notification_service = EmailNotificationService(self._email_gateway, self._configuration.email.sender, self._configuration.email.recipient)
        else:
            self._notification_service = None

    @property
    def configuration(self):
        return self._configuration

    def print_gcode(self, file_name, print_sub_layers=True, dry_run=False):
        self._current_file_name = file_name
        self._current_file = open(file_name, 'r')
        gcode_reader = GCodeReader(self._current_file, scale=self._configuration.options.scaling_factor, start_height=self._start_height)
        gcode_layer_generator = gcode_reader.get_layers()
        layer_generator = gcode_layer_generator
        self.print_layers(layer_generator, print_sub_layers, dry_run)

    def _get_zaxis(self, dry_run):
        if dry_run:
            return None
        elif self._configuration.dripper.dripper_type == 'photo':
            logger.info("Photo Zaxis")
            return PhotoZAxis(
                self._start_height,
                self._configuration.dripper.photo_zaxis_delay
                )
        elif self._configuration.dripper.dripper_type == 'emulated':
            logger.info("Emulated Zaxis")
            return TimedDripZAxis(
                self._configuration.dripper.drips_per_mm,
                self._start_height,
                drips_per_second=self._configuration.dripper.emulated_drips_per_second
                )
        elif self._configuration.dripper.dripper_type == 'microcontroller':
            logger.info("Micro Controller Zaxis")
            return SerialDripZAxis(
                self._get_communicator(dry_run),
                self._configuration.dripper.drips_per_mm,
                self._start_height,
                )

    def _get_communicator(self, dry_run):
        if hasattr(self, '_communicator'):
            return self._communicator
        if dry_run:
            self._communicator = NullCommunicator()
        else:
            self._communicator = SerialCommunicator(
                self._configuration.micro_com.port,
                self._configuration.micro_com.header,
                self._configuration.micro_com.footer,
                self._configuration.micro_com.escape,
                )
            self._communicator.start()
        return self._communicator

    def _get_digital_disseminator(self, dry_run):
            
            return MicroDisseminator(
                self.laser_control,
                self._get_communicator(dry_run),
                self._configuration.micro_com.rate
                )

    def print_layers(self, layer_generator, print_sub_layers=True, dry_run=False):
        logger.info("Shuffled: %s" % self._configuration.options.use_shufflelayers)
        logger.info("Sublayered: %s" % self._configuration.options.use_sublayers)
        logger.info("Overlapped: %s" % self._configuration.options.use_overlap)

        if self._configuration.options.use_sublayers and print_sub_layers:
            layer_generator = SubLayerGenerator(layer_generator, self._configuration.options.sublayer_height_mm)
        if self._configuration.options.use_shufflelayers:
            layer_generator = ShuffleGenerator(layer_generator, self._configuration.options.shuffle_layers_amount)
        if self._configuration.options.use_overlap:
            layer_generator = OverLapGenerator(layer_generator, self._configuration.options.overlap_amount)

        if self._configuration.serial.on:
            self._commander = SerialCommander(self._configuration.serial.port)
        else:
            self._commander = NullCommander()

        self.laser_control = LaserControl(self._configuration.cure_rate.override_laser_power_amount)

        transformer = HomogenousTransformer(
            self._configuration.calibration.max_deflection,
            self._configuration.calibration.height,
            self._configuration.calibration.lower_points,
            self._configuration.calibration.upper_points,
            )

        state = MachineState()
        self._status = MachineStatus(self._status_call_back)

        if dry_run:
            abort_on_error = False
        else:
            abort_on_error = True

        self._zaxis = self._get_zaxis(dry_run)

        if self._configuration.circut.circut_type == 'Digital':
            disseminator = self._get_digital_disseminator(dry_run)

        path_to_points = PathToPoints(
            disseminator.samples_per_second,
            transformer,
            self._configuration.options.laser_thickness_mm
            )

        override_speed = self._configuration.cure_rate.draw_speed if self._configuration.cure_rate.use_draw_speed else None
        pre_layer_delay = self._configuration.options.pre_layer_delay if self._configuration.options.pre_layer_delay else 0.0
        post_fire_delay_speed = None
        slew_delay_speed = None
        if self._configuration.options.post_fire_delay:
            post_fire_delay_speed = self._configuration.options.laser_thickness_mm / (float(self._configuration.options.post_fire_delay) / 1000.0)
        if self._configuration.options.slew_delay:
            slew_delay_speed = self._configuration.options.laser_thickness_mm / (float(self._configuration.options.slew_delay) / 1000.0)

        self._writer = LayerWriter(
            disseminator,
            path_to_points,
            self.laser_control,
            state,
            move_distance_to_ignore=self._configuration.options.laser_thickness_mm,
            override_speed=override_speed,
            wait_speed=self._configuration.options.laser_thickness_mm / (float(self._configuration.options.wait_after_move_milliseconds) / 1000.0),
            post_fire_delay_speed=post_fire_delay_speed,
            slew_delay_speed=slew_delay_speed
            )

        self._layer_processing = LayerProcessing(
            self._writer,
            state,
            self._status,
            self._zaxis,
            self._configuration.dripper.max_lead_distance_mm,
            self._commander,
            pre_layer_delay,
            self._configuration.serial.layer_started,
            self._configuration.serial.layer_ended,
            self._configuration.serial.print_ended,
            )

        if self._zaxis:
            self._zaxis.set_call_back(self._status.drip_call_back)
            self._zaxis.start()

        self._controller = Controller(
            self._writer,
            self._layer_processing,
            layer_generator,
            self._status,
            abort_on_error=abort_on_error,
            )

        self._controller.start()

    def get_status(self):
        return self._controller.get_status()

    def can_set_drips_per_second(self):
        if getattr(self._zaxis, 'set_drips_per_second', False):
            return True
        else:
            return False

    def set_drips_per_second(self, drips_per_second):
        if getattr(self._zaxis, 'set_drips_per_second', False):
            self._zaxis.set_drips_per_second(drips_per_second)
        else:
            logger.error('Cannot change drips per second on %s' % type(self._zaxis))
            raise Exception('Cannot change drips per second on %s' % type(self._zaxis))

    def get_drips_per_second(self):
        if getattr(self._zaxis, 'get_drips_per_second'):
            return self._zaxis.get_drips_per_second()
        else:
            logger.warning("Drips per second requested but does not exist")
            return 0.0

    def verify_gcode(self, file_name):
        self.print_gcode(file_name,  print_sub_layers=False,  dry_run=True)

    def close(self):
        if self._zaxis:
            self._zaxis.close()
        if self._controller:
            self._controller.close()
        else:
            logger.warning('Stopped before printing')
        if self._current_file:
            self._current_file.close()
            logger.info("File Closed")
        if self._notification_service:
            self._notification_service.send_message("Print Complete", "%s is complete" % self._current_file_name)
