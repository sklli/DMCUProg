# pyOCD debugger
# Copyright (c) 2019 Arm Limited
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from .interface import Interface
from .common import CMSIS_DAP_USB_CLASSES
from ..dap_access_api import DAPAccessIntf
import logging
import os
import threading
import six
from time import sleep
import errno

LOG = logging.getLogger(__name__)

try:
    import usb.core
    import usb.util
except:
    IS_AVAILABLE = False
else:
    IS_AVAILABLE = True

class PyUSBv2(Interface):
    """!
    @brief CMSIS-DAPv2 interface using pyUSB.
    """

    isAvailable = IS_AVAILABLE

    def __init__(self):
        super(PyUSBv2, self).__init__()
        self.ep_out = None
        self.ep_in = None
        self.ep_swo = None
        self.dev = None
        self.intf_number = None
        self.serial_number = None
        self.kernel_driver_was_attached = False
        self.closed = True
        self.thread = None
        self.rx_stop_event = None
        self.swo_thread = None
        self.swo_stop_event = None
        self.rcv_data = []
        self.swo_data = []
        self.read_sem = threading.Semaphore(0)
        self.packet_size = 512
        self.is_swo_running = False
    
    @property
    def has_swo_ep(self):
        return self.ep_swo is not None

    def open(self):
        assert self.closed is True

        # Get device handle
        dev = usb.core.find(custom_match=HasCmsisDapv2Interface(self.serial_number))
        if dev is None:
            raise DAPAccessIntf.DeviceError("Device %s not found" %
                                            self.serial_number)

        # get active config
        config = dev.get_active_configuration()

        # Get CMSIS-DAPv2 interface
        interface = usb.util.find_descriptor(config, custom_match=match_cmsis_dap_interface_name)
        if interface is None:
            raise DAPAccessIntf.DeviceError("Device %s has no CMSIS-DAPv2 interface" %
                                            self.serial_number)
        interface_number = interface.bInterfaceNumber

        # Find endpoints. CMSIS-DAPv2 endpoints are in a fixed order.
        try:
            ep_out = interface.endpoints()[0]
            ep_in = interface.endpoints()[1]
            ep_swo = interface.endpoints()[2] if len(interface.endpoints()) > 2 else None
        except IndexError:
            raise DAPAccessIntf.DeviceError("CMSIS-DAPv2 device %s is missing endpoints" %
                                            self.serial_number)

        # Explicitly claim the interface
        try:
            usb.util.claim_interface(dev, interface_number)
        except usb.core.USBError as exc:
            raise six.raise_from(DAPAccessIntf.DeviceError("Unable to open device"), exc)

        # Update all class variables if we made it here
        self.ep_out = ep_out
        self.ep_in = ep_in
        self.ep_swo = ep_swo
        self.dev = dev
        self.intf_number = interface_number

        # Start RX thread as the last step
        self.closed = False
        self.start_rx()

    def start_rx(self):
        # Flush the RX buffers by reading until timeout exception
        try:
            while True:
                self.ep_in.read(self.ep_in.wMaxPacketSize, 1)
        except usb.core.USBError:
            # USB timeout expected
            pass

        # Start RX thread
        self.rx_stop_event = threading.Event()
        thread_name = "CMSIS-DAP receive (%s)" % self.serial_number
        self.thread = threading.Thread(target=self.rx_task, name=thread_name)
        self.thread.daemon = True
        self.thread.start()
    
    def start_swo(self):
        self.swo_stop_event = threading.Event()
        thread_name = "SWO receive (%s)" % self.serial_number
        self.swo_thread = threading.Thread(target=self.swo_rx_task, name=thread_name)
        self.swo_thread.daemon = True
        self.swo_thread.start()
        self.is_swo_running = True
    
    def stop_swo(self):
        self.swo_stop_event.set()
        self.swo_thread.join()
        self.swo_thread = None
        self.swo_stop_event = None
        self.is_swo_running = False

    def rx_task(self):
        try:
            while not self.rx_stop_event.is_set():
                self.read_sem.acquire()
                if not self.rx_stop_event.is_set():
                    self.rcv_data.append(self.ep_in.read(self.ep_in.wMaxPacketSize, 10 * 1000))
        finally:
            # Set last element of rcv_data to None on exit
            self.rcv_data.append(None)

    def swo_rx_task(self):
        try:
            while not self.swo_stop_event.is_set():
                try:
                    self.swo_data.append(self.ep_swo.read(self.ep_swo.wMaxPacketSize, 10 * 1000))
                except usb.core.USBError:
                    pass
        finally:
            # Set last element of swo_data to None on exit
            self.swo_data.append(None)

    @staticmethod
    def get_all_connected_interfaces():
        """! @brief Returns all the connected devices with a CMSIS-DAPv2 interface."""
        # find all cmsis-dap devices
        try:
            all_devices = usb.core.find(find_all=True, custom_match=HasCmsisDapv2Interface())
        except usb.core.NoBackendError:
            # Print a warning if pyusb cannot find a backend, and return no probes.
            LOG.warning("CMSIS-DAPv2 probes are not supported because no libusb library was found.")
            return []

        # iterate on all devices found
        boards = []
        for board in all_devices:
            new_board = PyUSBv2()
            new_board.vid = board.idVendor
            new_board.pid = board.idProduct
            new_board.product_name = board.product
            new_board.vendor_name = board.manufacturer
            new_board.serial_number = board.serial_number
            boards.append(new_board)

        return boards

    def write(self, data):
        """! @brief Write data on the OUT endpoint."""

        report_size = self.packet_size
        if self.ep_out:
            report_size = self.ep_out.wMaxPacketSize

        for _ in range(report_size - len(data)):
            data.append(0)

        self.read_sem.release()

        self.ep_out.write(data)
        #logging.debug('sent: %s', data)

    def read(self):
        """! @brief Read data on the IN endpoint."""
        while len(self.rcv_data) == 0:
            sleep(0)

        if self.rcv_data[0] is None:
            raise DAPAccessIntf.DeviceError("Device %s read thread exited unexpectedly" % self.serial_number)
        return self.rcv_data.pop(0)

    def read_swo(self):
        # Accumulate all available SWO data.
        data = bytearray()
        while len(self.swo_data):
            if self.swo_data[0] is None:
                raise DAPAccessIntf.DeviceError("Device %s SWO thread exited unexpectedly" % self.serial_number)
            data += self.swo_data.pop(0)
        
        return data

    def set_packet_count(self, count):
        # No interface level restrictions on count
        self.packet_count = count

    def set_packet_size(self, size):
        self.packet_size = size

    def get_serial_number(self):
        return self.serial_number

    def close(self):
        """! @brief Close the USB interface."""
        assert self.closed is False

        if self.is_swo_running:
            self.stop_swo()
        self.closed = True
        self.rx_stop_event.set()
        self.read_sem.release()
        self.thread.join()
        assert self.rcv_data[-1] is None
        self.rcv_data = []
        self.swo_data = []
        usb.util.release_interface(self.dev, self.intf_number)
        usb.util.dispose_resources(self.dev)
        self.ep_out = None
        self.ep_in = None
        self.ep_swo = None
        self.dev = None
        self.intf_number = None
        self.thread = None

def match_cmsis_dap_interface_name(desc):
    interface_name = usb.util.get_string(desc.device, desc.iInterface)
    return (interface_name is not None) and ("CMSIS-DAP" in interface_name)

class HasCmsisDapv2Interface(object):
    """! @brief CMSIS-DAPv2 match class to be used with usb.core.find"""

    def __init__(self, serial=None):
        """! @brief Create a new FindDap object with an optional serial number"""
        self._serial = serial

    def __call__(self, dev):
        """! @brief Return True if this is a CMSIS-DAPv2 device, False otherwise"""
        # Check if the device class is a valid one for CMSIS-DAP.
        if dev.bDeviceClass not in CMSIS_DAP_USB_CLASSES:
            return False
        
        try:
            config = dev.get_active_configuration()
            cmsis_dap_interface = usb.util.find_descriptor(config, custom_match=match_cmsis_dap_interface_name)
        except OSError as error:
            if error.errno == errno.EACCES:
                LOG.debug(("Error \"{}\" while trying to access the USB device configuration "
                   "for VID=0x{:04x} PID=0x{:04x}. This can probably be remedied with a udev rule.")
                   .format(error, dev.idVendor, dev.idProduct))
            else:
                LOG.warning("OS error getting USB interface string: %s", error)
            return False
        except usb.core.USBError as error:
            LOG.warning("Exception getting product string: %s", error)
            return False
        except IndexError as error:
            LOG.warning("Internal pyusb error: %s", error)
            return False
        except NotImplementedError as error:
            LOG.debug("Received USB unimplemented error (VID=%04x PID=%04x)", dev.idVendor, dev.idProduct)
            return False

        if cmsis_dap_interface is None:
            return False
        
        # Check the class and subclass are vendor-specific.
        if (cmsis_dap_interface.bInterfaceClass != 0xff) or (cmsis_dap_interface.bInterfaceSubClass != 0):
            return False

        if self._serial is not None:
            if self._serial != dev.serial_number:
                return False
        return True