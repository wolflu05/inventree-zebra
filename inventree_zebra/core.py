"""InvenTree Zebra driver for label printing support"""

from typing import cast
from django.db.models.base import Model as Model
from django.db.models.query import QuerySet
from django.http import JsonResponse
from django.utils.translation import gettext_lazy as _
from django.core.validators import MinValueValidator, MaxValueValidator
import serial
import zpl

from plugin import InvenTreePlugin
from plugin.mixins import MachineDriverMixin
from plugin.machine import MachineProperty
from plugin.machine.machine_types import LabelPrinterBaseDriver, LabelPrinterMachine
from report.models import LabelTemplate

from . import PLUGIN_VERSION


class InvenTreeZebra(MachineDriverMixin, InvenTreePlugin):
    """InvenTreeZebra - custom InvenTree plugin."""

    # Plugin metadata
    TITLE = "InvenTree Zebra"
    NAME = "InvenTreeZebra"
    SLUG = "inventree-zebra"
    DESCRIPTION = "InvenTree Zebra driver for label printing support"
    VERSION = PLUGIN_VERSION

    # Additional project information
    AUTHOR = "wolflu05"
    WEBSITE = "https://github.com/wolflu05/inventree-zebra"
    LICENSE = "GPL-3.0+"

    # Optionally specify supported InvenTree versions
    MIN_VERSION = "1.1.0"
    # MAX_VERSION = '2.0.0'

    def get_machine_drivers(self):
        """Register machine drivers."""
        return [ZebraLabelPrintingTCPDriver, ZebraLabelPrintingSerialDriver]


class ZebraLabelPrintingBaseDriver(LabelPrinterBaseDriver):
    """Zebra label printing driver base class"""

    def __init__(self, *args, **kwargs):

        # Append settings already set by the derived driver
        self.MACHINE_SETTINGS["THRESHOLD"] = {
            "name": _("Threshold"),
            "description": _(
                "Set the threshold for converting grayscale to BW (0-255)"
            ),
            "validator": [int, MinValueValidator(0), MaxValueValidator(255)],
            "default": 200,
            "required": True,
        }

        self.MACHINE_SETTINGS["DARKNESS"] = {
            "name": _("Darkness"),
            "description": _("Set the darkness level (0-30)"),
            "validator": [int, MinValueValidator(0), MaxValueValidator(30)],
            "default": 20,
            "required": True,
        }

        self.MACHINE_SETTINGS["DPMM"] = {
            "name": _("Dots per mm"),
            "description": _("Set the printing resolution (dots per mm)"),
            "choices": [
                ("8", "8 dots per mm (203dpi)"),
                ("12", "12 dots per mm (300dpi)"),
                ("24", "24 dots per mm (600dpi)"),
            ],
            "default": "8",
            "required": True,
        }

        self.MACHINE_SETTINGS["PRINTER_INIT"] = {
            "name": _("Printer Initialization"),
            "description": _(
                "Additional ZPL commands to initialize the printer before each print job"
            ),
            "default": "~TA000~JSN^LT0^MNW^MTT^PMN^PON^PR2,2^LRN",
            "required": True,
        }

        super().__init__(*args, **kwargs)

    def restart_machine(self, machine):
        """Inventree Driver API: Restart machine"""
        self.update_status(machine)

    def ping_machines(self):
        """Ping all configured Zebra printers to check if they are online."""
        for machine in cast(list[LabelPrinterMachine], self.get_machines()):
            self.update_status(machine)

    def update_status(self, machine: BaseMachineType):
        """Update Zebra printer status"""
        try:
            printer = self.get_zpl_printer(machine)
            _, warnings, _, errors = printer.get_printer_errors()
            info = printer.get_printer_info()
            status = printer.get_printer_status()

            properties: list[MachineProperty] = [
                {"key": "Model", "value": info.get("model", "Unknown")},
                {"key": "Firmware", "value": info.get("version", "Unknown")},
                {"key": "Serial", "value": printer.get_sn()},
                {"key": "MAC", "value": printer.get_mac()},
                {"key": "DPMM", "value": info.get("dpmm", "Unknown")},
                {"key": "Memory", "value": info.get("mem", "Unknown")},
                {
                    "group": "Printer Status",
                    "key": "Head down",
                    "type": "bool",
                    "value": status.get("head_up", "0") != "1",
                },
                {
                    "group": "Printer Status",
                    "key": "Cover closed",
                    "type": "bool",
                    "value": printer.request_sdg("sensor.cover_open") != "yes",
                },
                {
                    "group": "Printer Status",
                    "key": "Paper",
                    "type": "bool",
                    "value": status.get("paper_out", "0") != "1",
                },
                {
                    "group": "Printer Status",
                    "key": "Ribbon",
                    "type": "bool",
                    "value": status.get("ribbon_out", "0") != "1",
                },
                {
                    "group": "Printer Status",
                    "key": "Not Paused",
                    "type": "bool",
                    "value": status.get("pause", "0") != "1",
                },
                {
                    "group": "Printer Status",
                    "key": "Print length",
                    "type": "str",
                    "value": printer.request_sdg(
                        "odometer.total_print_length"
                    ).split(",")[1],
                },
            ]

            machine.set_properties(properties)

            if len(warnings) > 0 or len(errors) > 0:
                machine.set_status(LabelPrinterMachine.MACHINE_STATUS.ERROR)
            else:
                machine.set_status(LabelPrinterMachine.MACHINE_STATUS.CONNECTED)

            machine.reset_errors()
            if len(errors) > 0:
                machine.handle_error(", ".join(errors))

            machine.set_status_text(", ".join(warnings))

        except Exception as e:
            machine.set_status(LabelPrinterMachine.MACHINE_STATUS.DISCONNECTED)
            machine.reset_errors()
            machine.handle_error(str(e))

    def print_labels(
        self,
        machine: LabelPrinterMachine,
        label: LabelTemplate,
        items: QuerySet[Model],
        **kwargs,
    ) -> JsonResponse | None:
        """Print the specified label to the specified machine."""

        printer = self.get_zpl_printer(machine)

        printing_options = kwargs.get("printing_options", {})

        threshold = cast(int, machine.get_setting("THRESHOLD", "D"))
        darkness = cast(int, machine.get_setting("DARKNESS", "D"))
        dpmm = int(cast(str, machine.get_setting("DPMM", "D")))
        dpi = {8: 203, 12: 300, 24: 600}.get(dpmm, 203)
        printer_init = cast(str, machine.get_setting("PRINTER_INIT", "D"))
        width, height = round(label.width), round(label.height)

        for item in items:
            png = self.render_to_png(label, item, dpi=dpi)
            if png is None:
                continue
            data = png.convert("L").point(
                lambda x: 255 if x > threshold else 0,  # type: ignore
                mode="1",
            )

            lb = zpl.Label(height=height, width=width, dpmm=dpmm)
            lb.set_darkness(darkness)
            lb.labelhome(0, 0)
            lb.zpl_raw(printer_init)
            lb.origin(0, 0)
            lb.zpl_raw("^PQ" + str(printing_options.get("copies", 1)))
            lb.write_graphic(data, width)
            lb.endorigin()

            printer.send_job(lb.dumpZPL())

class ZebraLabelPrintingTCPDriver(ZebraLabelPrintingBaseDriver):
    """Zebra label printing over TCP driver for InvenTree."""

    SLUG = "zebra-driver"
    NAME = "Zebra TCP Driver"
    DESCRIPTION = "Zebra label printing over TCP driver for InvenTree"

    def __init__(self, *args, **kwargs):
        self.MACHINE_SETTINGS = {
            "HOST": {
                "name": _("Host"),
                "description": _("IP/Hostname"),
                "default": "",
                "required": True,
            },
            "PORT": {
                "name": _("Port"),
                "description": _("Port number"),
                "validator": [int, MinValueValidator(1), MaxValueValidator(65535)],
                "default": 9100,
                "required": True,
            },
        }

        super().__init__(*args, **kwargs)

    def get_zpl_printer(self, machine: LabelPrinterMachine) -> "TCPPrinterEnhanced":
        """Return a zpl.Printer instance for the specified machine."""
        host = cast(str, machine.get_setting("HOST", "D"))
        port = cast(int, machine.get_setting("PORT", "D"))
        return TCPPrinterEnhanced(host, port)

class TCPPrinterEnhanced(zpl.TCPPrinter):
    """Enhanced TCPPrinter."""

    def request_sdg(self, key):
        """Request specific SDG key."""
        self.socket.sendall(f'! U1 getvar "{key}"\r\n'.encode("utf-8"))
        x = self.socket.recv(1024).decode("utf-8")
        x = x.strip().removeprefix('"').removesuffix('"')
        return x

class ZebraLabelPrintingSerialDriver(ZebraLabelPrintingBaseDriver):
    """Zebra label printing driver over Serial for InvenTree."""

    SLUG = "zebra-serial-driver"
    NAME = "Zebra Serial Driver"
    DESCRIPTION = "Zebra label printing over Serial driver for InvenTree"

    def __init__(self, *args, **kwargs):
        self.MACHINE_SETTINGS = {
            "PORT": {
                "name": _("Port"),
                "description": _("Serial port"),
                "default": "/dev/ttyUSB0",
                "required": True,
            },
            "BAUD": {
                "name": _("Baud Rate"),
                "description": _("Serial port baud rate"),
                "choices": [
                    ("110", "110 Baud"),
                    ("300", "300 Baud"),
                    ("600", "600 Baud"),
                    ("2400", "2400 Baud"),
                    ("4800", "4800 Baud"),
                    ("9600", "9600 Baud"),
                    ("14400", "14400 Baud"),
                    ("19200", "19200 Baud"),
                    ("28800", "28800 Baud"),
                    ("38400", "38400 Baud"),
                    ("57600", "57600 Baud"),
                    ("115200", "115200 Baud")
                ],
                "default": "9600",
                "required": True,
            },
        }

        super().__init__(*args, **kwargs)

    def get_zpl_printer(self, machine: LabelPrinterMachine) -> "SerialPrinter":
        """Return a zpl.Printer instance for the specified machine."""
        port = cast(str, machine.get_setting("PORT", "D"))
        baud = cast(int, machine.get_setting("BAUD", "D"))
        return SerialPrinter(port, baud)

class SerialPrinter(zpl.Printer):
    """ZPL Printer driver for serial connected device"""

    def __init__(self, port, baud):
        try:
            self.serial = serial.Serial(port, baud, timeout=2)
        except:
            raise
        finally:
            super().__init__()

    def send_job(self, zpl2):
        if isinstance(zpl2, zpl.label.Label):
            self.serial.write(b'\x02')
            self.serial.write(zpl2.dumpZPL())
            self.serial.write(b'\x03')
        else:
            self.serial.write(b'\x02')
            self.serial.write(zpl2.encode())
            self.serial.write(b'\x03')

    def request_info(self, command):
        self.serial.reset_input_buffer()
        self.serial.write(b'\x02')
        self.serial.write(command.encode())
        self.serial.write(b'\x03')

        lineCount = 1
        if ('~HS' == command): lineCount = 3

        buf = b''
        for n in range(lineCount):
            buf += self.serial.read_until(b'\x03')

        return buf

    def request_sdg(self, key):
        """Request specific SDG key."""
        self.serial.reset_input_buffer()
        self.serial.write(f'! U1 getvar "{key}"\r\n'.encode())
        buf = self.serial.readline().decode()
        buf = buf.strip().removeprefix('"').removesuffix('"')

        return buf

    def __del__(self):
        if 'self.serial' in locals():
            self.serial.close()
