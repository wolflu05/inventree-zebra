"""InvenTree Zebra driver for label printing support"""

from typing import cast
from django.db.models.base import Model as Model
from django.db.models.query import QuerySet
from django.http import JsonResponse
from django.utils.translation import gettext_lazy as _
from django.core.validators import MinValueValidator, MaxValueValidator
import zpl
import time
from collections import deque
import structlog

from plugin import InvenTreePlugin
from plugin.mixins import MachineDriverMixin
from plugin.machine import MachineProperty
from plugin.machine.machine_types import LabelPrinterBaseDriver, LabelPrinterMachine
from report.models import LabelTemplate, DataOutput

from . import PLUGIN_VERSION


logger = structlog.get_logger("inventree-zebra")


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
            "MIN_QUEUE_SIZE": {
                "name": _("Minimum Queue Size"),
                "description": _(
                    "Minimum number of labels to generate before sending to printer"
                ),
                "validator": [int, MinValueValidator(1)],
                "default": 1,
                "required": True,
            },
            "MAX_QUEUE_SIZE": {
                "name": _("Maximum Queue Size"),
                "description": _(
                    "Maximum number of labels to generate before sending to printer"
                ),
                "validator": [int, MinValueValidator(1)],
                "default": 50,
                "required": True,
            },
            "MAX_LABELS_IN_PRINTER_QUEUE": {
                "name": _("Max Labels in Printer Queue"),
                "description": _(
                    "Maximum number of labels to keep in the printer queue at once"
                ),
                "validator": [int, MinValueValidator(1)],
                "default": 5,
                "required": True,
            },
            "PRINTER_POLL_INTERVAL": {
                "name": _("Printer Poll Interval"),
                "description": _(
                    "Interval (in ms) to poll printer status during printing"
                ),
                "validator": [int, MinValueValidator(1)],
                "default": 500,
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
                    {
                        "group": "Printer Status",
                        "key": "Total labels",
                        "type": "int",
                        "value": self.get_total_labels(printer) or -1,
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

        output = cast(DataOutput, kwargs.get("output", None))
        items_list = list(items)

        def generate_label(idx: int):
            item = items_list[idx]
            png = self.render_to_png(label, item, dpi=dpi)
            if png is None:
                return None
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

            # suppress forward feed between labels if there are multiple items to print
            if idx < len(items_list) - 1:
                lb.zpl_raw("^XB")

            lb.write_graphic(data, width)
            lb.endorigin()

            return lb.dumpZPL()

        queue = deque()
        generate_idx = 0
        sent_idx = 0
        total_labels = self.get_total_labels(printer)
        labels_in_printer_queue = 0

        MIN_QUEUE_SIZE = cast(int, machine.get_setting("MIN_QUEUE_SIZE", "D"))
        MAX_QUEUE_SIZE = cast(int, machine.get_setting("MAX_QUEUE_SIZE", "D"))
        MAX_LABELS_IN_PRINTER_QUEUE = cast(
            int, machine.get_setting("MAX_LABELS_IN_PRINTER_QUEUE", "D")
        )
        POLL_INTERVAL = (
            cast(int, machine.get_setting("PRINTER_POLL_INTERVAL", "D")) / 1000.0
        )
        last_poll = time.monotonic()

        while (
            generate_idx < len(items_list)
            or len(queue) > 0
            or labels_in_printer_queue > 0
        ):
            # fill the queue with up to 50 labels as long as we have some spare time
            while (
                generate_idx < len(items_list)
                and len(queue) < MAX_QUEUE_SIZE
                and (
                    (time.monotonic() - last_poll) < POLL_INTERVAL
                    or len(queue) < MIN_QUEUE_SIZE
                )
            ):
                queue.append(generate_label(generate_idx))
                generate_idx += 1
                logger.debug(
                    f"[GEN] generated label zpl {generate_idx}/{len(items_list)}"
                )

            # get printer total labels count
            now = time.monotonic()
            if now - last_poll >= POLL_INTERVAL:
                current_total = self.get_total_labels(printer)
                labels_in_printer_queue = 0
                if total_labels is not None and current_total is not None:
                    already_printed = max(0, current_total - total_labels)
                    labels_in_printer_queue = max(0, sent_idx - already_printed)

                    logger.debug(
                        f"[ODO] Printed labels: {already_printed}, labels in printer queue: {labels_in_printer_queue}, total printed: {current_total}"
                    )

                    if output:
                        output.progress = already_printed
                        output.save()
                else:
                    logger.debug("[ODO] Unable to get total labels from printer")

                # send up to 5 labels to the printer if there is room in the printer queue
                if (
                    labels_in_printer_queue < MAX_LABELS_IN_PRINTER_QUEUE
                    and len(queue) > 0
                ):
                    batch_size = min(
                        MAX_LABELS_IN_PRINTER_QUEUE - labels_in_printer_queue,
                        len(queue),
                    )
                    batch = [queue.popleft() for _ in range(batch_size)]

                    logger.debug(
                        f"[SEND] send batch_size={len(batch)} to printer, queue_size={len(queue)}"
                    )

                    printer.send_job("".join(batch))
                    sent_idx += len(batch)

                    logger.debug(f"[SEND] {sent_idx}/{len(items_list)}")

                last_poll = now

            # sleep only if:
            # - all labels have been generated
            # - poll is not due yet
            elif generate_idx >= len(items_list):
                remaining_time = POLL_INTERVAL - (now - last_poll)
                logger.debug(
                    f"Waiting {remaining_time:.2f} seconds for printer status update..."
                )
                time.sleep(remaining_time)

    def get_total_labels(self, printer: "TCPPrinterEnhanced") -> int | None:
        """Get the total number of labels printed by the printer."""
        try:
            total = printer.request_sdg("odometer.total_label_count")
            return int(total)
        except Exception:
            return None


class TCPPrinterEnhanced(zpl.TCPPrinter):
    """Enhanced TCPPrinter."""

    def request_sdg(self, key):
        """Request specific SDG key."""
        self.socket.sendall(f'! U1 getvar "{key}"\r\n'.encode("utf-8"))
        x = self.socket.recv(1024).decode("utf-8")
        x = x.strip().removeprefix('"').removesuffix('"')
        return x
