"""InvenTree Zebra driver for label printing support"""

from typing import cast
from django.db.models.base import Model as Model
from django.db.models.query import QuerySet
from django.http import JsonResponse
from django.utils.translation import gettext_lazy as _
from django.core.validators import MinValueValidator, MaxValueValidator
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
        return [ZebraLabelPrintingDriver]


class ZebraLabelPrintingDriver(LabelPrinterBaseDriver):
    """Zebra label printing driver for InvenTree."""

    SLUG = "zebra-driver"
    NAME = "Zebra Driver"
    DESCRIPTION = "Zebra label printing driver for InvenTree"

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
            "THRESHOLD": {
                "name": _("Threshold"),
                "description": _(
                    "Set the threshold for converting grayscale to BW (0-255)"
                ),
                "validator": [int, MinValueValidator(0), MaxValueValidator(255)],
                "default": 200,
                "required": True,
            },
            "DARKNESS": {
                "name": _("Darkness"),
                "description": _("Set the darkness level (0-30)"),
                "validator": [int, MinValueValidator(0), MaxValueValidator(30)],
                "default": 20,
                "required": True,
            },
            "DPMM": {
                "name": _("Dots per mm"),
                "description": _("Set the printing resolution (dots per mm)"),
                "choices": [
                    ("8", "8 dots per mm (203dpi)"),
                    ("12", "12 dots per mm (300dpi)"),
                    ("24", "24 dots per mm (600dpi)"),
                ],
                "default": "8",
                "required": True,
            },
            "PRINTER_INIT": {
                "name": _("Printer Initialization"),
                "description": _(
                    "Additional ZPL commands to initialize the printer before each print job"
                ),
                "default": "~TA000~JSN^LT0^MNW^MTT^PMN^PON^PR2,2^LRN",
                "required": True,
            },
        }

        super().__init__(*args, **kwargs)

    def get_zpl_printer(self, machine: LabelPrinterMachine) -> "TCPPrinterEnhanced":
        """Return a zpl.Printer instance for the specified machine."""
        host = cast(str, machine.get_setting("HOST", "D"))
        port = cast(int, machine.get_setting("PORT", "D"))
        return TCPPrinterEnhanced(host, port)

    def ping_machines(self):
        """Ping all configured Zebra printers to check if they are online."""
        for machine in cast(list[LabelPrinterMachine], self.get_machines()):
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

        try:
            zpl_template = label.metadata['zpl_template']
        except Exception:
            zpl_template = False


        for item in items:


            if zpl_template:
                data = kwargs['context']['template'].render_as_string(kwargs['item_instance'], None).replace('\n', '')
            else:
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
            if zpl_template:
                lb.zpl_raw(data)
            else:
                lb.write_graphic(data, width)
            lb.endorigin()

            printer.send_job(lb.dumpZPL())


class TCPPrinterEnhanced(zpl.TCPPrinter):
    """Enhanced TCPPrinter."""

    def request_sdg(self, key):
        """Request specific SDG key."""
        self.socket.sendall(f'! U1 getvar "{key}"\r\n'.encode("utf-8"))
        x = self.socket.recv(1024).decode("utf-8")
        x = x.strip().removeprefix('"').removesuffix('"')
        return x
