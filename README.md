# inventree-zebra

[![License: ](https://img.shields.io/badge/License-GPLv3-yellow.svg)](https://opensource.org/license/gpl-3-0)
![CI](https://github.com/wolflu05/inventree-zebra/actions/workflows/ci.yml/badge.svg)

A zebra label printer driver plugin compatible with the [InvenTree](https://inventree.org/) Machine Registry.

![Printer Settings](https://github.com/user-attachments/assets/82d3beaa-f837-45b4-99e5-17e3140a8116)

## Installation

> [!IMPORTANT]
> This plugin is only compatible with InvenTree>=1.1.0

Goto "Admin Center > Plugins > Install Plugin" and enter `inventree-zebra` as package name and activate it.

Then goto "Admin Center > Machines" and create a new machine using this driver.


## ZPL Labels
You can write a ZPL template and upload
it to the InvenTree Label templates as usual. Add a command to the template's metadata:

```
{"zpl_template": "True"}
```

This causes the printer driver to ignores the picture rendered by WeasyPrint. Instead it calls the `render_to_string` function of the template and sends the
result to the printer.
