# -*- coding: utf-8 -*-
import commands
import logging
import simplejson
import os
import os.path
import io
import base64
import openerp
import time
import random
import math
import md5
import openerp.addons.hw_proxy.controllers.main as hw_proxy
import pickle
import re
import subprocess
import traceback

try: 
    from .. escpos import *
    from .. escpos.exceptions import *
    from .. escpos.printer import Usb
except ImportError:
    escpos = printer = None

from threading import Thread, Lock
from Queue import Queue, Empty

try:
    import usb.core
except ImportError:
    usb = None

from PIL import Image

from openerp import http
from openerp.http import request
from openerp.tools.translate import _

from unidecode import unidecode
from serial import Serial
from openerp.tools.config import config

_logger = logging.getLogger(__name__)

from os import listdir
from os.path import join
from select import select
try:
    import evdev
except ImportError:
    _logger.error('Odoo module hw_scanner depends on the evdev python module')
    evdev = None

drivers = {}

class EscposDriver(Thread):
    def __init__(self):
        Thread.__init__(self)
        self.queue = Queue()
        self.lock  = Lock()
        self.status = {'status':'connecting', 'messages':[]}

    def supported_devices(self):
        if not os.path.isfile('escpos_devices.pickle'):
            return supported_devices.device_list
        else:
            try:
                f = open('escpos_devices.pickle','r')
                return pickle.load(f)
                f.close()
            except Exception as e:
                self.set_status('error',str(e))
                return supported_devices.device_list

    def add_supported_device(self,device_string):
        r = re.compile('[0-9A-Fa-f]{4}:[0-9A-Fa-f]{4}');
        match = r.search(device_string)
        if match:
            match = match.group().split(':')
            vendor = int(match[0],16)
            product = int(match[1],16)
            name = device_string.split('ID')
            if len(name) >= 2:
                name = name[1]
            else:
                name = name[0]
            _logger.info('ESC/POS: adding support for device: '+match[0]+':'+match[1]+' '+name)
            
            device_list = supported_devices.device_list[:]
            if os.path.isfile('escpos_devices.pickle'):
                try:
                    f = open('escpos_devices.pickle','r')
                    device_list = pickle.load(f)
                    f.close()
                except Exception as e:
                    self.set_status('error',str(e))
            device_list.append({
                'vendor': vendor,
                'product': product,
                'name': name,
            })

            try:
                f = open('escpos_devices.pickle','w+')
                f.seek(0)
                pickle.dump(device_list,f)
                f.close()
            except Exception as e:
                self.set_status('error',str(e))

    def connected_usb_devices(self):
        connected = []
        
        for device in self.supported_devices():
            if usb.core.find(idVendor=device['vendor'], idProduct=device['product']) != None:
                connected.append(device)
        return connected

    def lockedstart(self):
        with self.lock:
            if not self.isAlive():
                self.daemon = True
                self.start()
    
    def get_escpos_printer(self):
  
        printers = self.connected_usb_devices()
        if len(printers) > 0:
            self.set_status('connected','Connected to '+printers[0]['name'])
            return Usb(printers[0]['vendor'], printers[0]['product'])
        else:
            self.set_status('disconnected','Printer Not Found')
            return None

    def get_status(self):
        self.push_task('status')
        return self.status

    def open_cashbox(self,printer):
        printer.cashdraw(2)
        printer.cashdraw(5)

    def set_status(self, status, message = None):
        _logger.info(status+' : '+ (message or 'no message'))
        if status == self.status['status']:
            if message != None and (len(self.status['messages']) == 0 or message != self.status['messages'][-1]):
                self.status['messages'].append(message)
        else:
            self.status['status'] = status
            if message:
                self.status['messages'] = [message]
            else:
                self.status['messages'] = []

        if status == 'error' and message:
            _logger.error('ESC/POS Error: '+message)
        elif status == 'disconnected' and message:
            _logger.warning('ESC/POS Device Disconnected: '+message)

    def run(self):

        if not escpos:
            _logger.error('ESC/POS cannot initialize, please verify system dependencies.')
            return
        while True:
            try:
                error = True
                timestamp, task, data = self.queue.get(True)

                printer = self.get_escpos_printer()

                if printer == None:
                    if task != 'status':
                        self.queue.put((timestamp,task,data))
                    error = False
                    time.sleep(5)
                    continue
                elif task == 'receipt': 
                    if timestamp >= time.time() - 1 * 60 * 60:
                        self.print_receipt_body(printer,data)
                        printer.cut()
                elif task == 'xml_receipt':
                    if timestamp >= time.time() - 1 * 60 * 60:
                        printer.receipt(data)
                elif task == 'cashbox':
                    if timestamp >= time.time() - 12:
                        self.open_cashbox(printer)
                elif task == 'printstatus':
                    self.print_status(printer)
                elif task == 'status':
                    pass
                error = False

            except NoDeviceError as e:
                print "No device found %s" %str(e)
            except HandleDeviceError as e:
                print "Impossible to handle the device due to previous error %s" % str(e)
            except TicketNotPrinted as e:
                print "The ticket does not seems to have been fully printed %s" % str(e)
            except NoStatusError as e:
                print "Impossible to get the status of the printer %s" % str(e)
            except Exception as e:
                self.set_status('error', str(e))
                errmsg = str(e) + '\n' + '-'*60+'\n' + traceback.format_exc() + '-'*60 + '\n'
                _logger.error(errmsg);
            finally:
                if error: 
                    self.queue.put((timestamp, task, data))
                if printer:
                    printer.close()

    def push_task(self,task, data = None):
        self.lockedstart()
        self.queue.put((time.time(),task,data))

    def print_status(self,eprint):
        localips = ['0.0.0.0','127.0.0.1','127.0.1.1']
        ips =  [ c.split(':')[1].split(' ')[0] for c in commands.getoutput("/sbin/ifconfig").split('\n') if 'inet addr' in c ]
        ips =  [ ip for ip in ips if ip not in localips ] 
        eprint.text('\n\n')
        eprint.set(align='center',type='b',height=2,width=2)
        eprint.text('PosBox Status\n')
        eprint.text('\n')
        eprint.set(align='center')

        if len(ips) == 0:
            eprint.text('ERROR: Could not connect to LAN\n\nPlease check that the PosBox is correc-\ntly connected with a network cable,\n that the LAN is setup with DHCP, and\nthat network addresses are available')
        elif len(ips) == 1:
            eprint.text('IP Address:\n'+ips[0]+'\n')
        else:
            eprint.text('IP Addresses:\n')
            for ip in ips:
                eprint.text(ip+'\n')

        if len(ips) >= 1:
            eprint.text('\nHomepage:\nhttp://'+ips[0]+':8069\n')

        eprint.text('\n\n')
        eprint.cut()

    def print_receipt_body(self,eprint,receipt):

        def check(string):
            return string != True and bool(string) and string.strip()
        
        def price(amount):
            return ("{0:."+str(receipt['precision']['price'])+"f}").format(amount)
        
        def money(amount):
            return ("{0:."+str(receipt['precision']['money'])+"f}").format(amount)

        def quantity(amount):
            if math.floor(amount) != amount:
                return ("{0:."+str(receipt['precision']['quantity'])+"f}").format(amount)
            else:
                return str(amount)

        def printline(left, right='', width=40, ratio=0.5, indent=0):
            lwidth = int(width * ratio) 
            rwidth = width - lwidth 
            lwidth = lwidth - indent
            
            left = left[:lwidth]
            if len(left) != lwidth:
                left = left + ' ' * (lwidth - len(left))

            right = right[-rwidth:]
            if len(right) != rwidth:
                right = ' ' * (rwidth - len(right)) + right

            return ' ' * indent + left + right + '\n'
        
        def print_taxes():
            taxes = receipt['tax_details']
            for tax in taxes:
                eprint.text(printline(tax['tax']['name'],price(tax['amount']), width=40,ratio=0.6))

        # Receipt Header
        if receipt['company']['logo']:
            eprint.set(align='center')
            eprint.print_base64_image(receipt['company']['logo'])
            eprint.text('\n')
        else:
            eprint.set(align='center',type='b',height=2,width=2)
            eprint.text(receipt['company']['name'] + '\n')

        eprint.set(align='center',type='b')
        if check(receipt['company']['contact_address']):
            eprint.text(receipt['company']['contact_address'] + '\n')
        if check(receipt['company']['phone']):
            eprint.text('Tel:' + receipt['company']['phone'] + '\n')
        if check(receipt['company']['vat']):
            eprint.text('VAT:' + receipt['company']['vat'] + '\n')
        if check(receipt['company']['email']):
            eprint.text(receipt['company']['email'] + '\n')
        if check(receipt['company']['website']):
            eprint.text(receipt['company']['website'] + '\n')
        if check(receipt['header']):
            eprint.text(receipt['header']+'\n')
        if check(receipt['cashier']):
            eprint.text('-'*32+'\n')
            eprint.text('Served by '+receipt['cashier']+'\n')

        # Orderlines
        eprint.text('\n\n')
        eprint.set(align='center')
        for line in receipt['orderlines']:
            pricestr = price(line['price_display'])
            if line['discount'] == 0 and line['unit_name'] == 'Unit(s)' and line['quantity'] == 1:
                eprint.text(printline(line['product_name'],pricestr,ratio=0.6))
            else:
                eprint.text(printline(line['product_name'],ratio=0.6))
                if line['discount'] != 0:
                    eprint.text(printline('Discount: '+str(line['discount'])+'%', ratio=0.6, indent=2))
                if line['unit_name'] == 'Unit(s)':
                    eprint.text( printline( quantity(line['quantity']) + ' x ' + price(line['price']), pricestr, ratio=0.6, indent=2))
                else:
                    eprint.text( printline( quantity(line['quantity']) + line['unit_name'] + ' x ' + price(line['price']), pricestr, ratio=0.6, indent=2))

        # Subtotal if the taxes are not included
        taxincluded = True
        if money(receipt['subtotal']) != money(receipt['total_with_tax']):
            eprint.text(printline('','-------'));
            eprint.text(printline(_('Subtotal'),money(receipt['subtotal']),width=40, ratio=0.6))
            print_taxes()
            #eprint.text(printline(_('Taxes'),money(receipt['total_tax']),width=40, ratio=0.6))
            taxincluded = False


        # Total
        eprint.text(printline('','-------'));
        eprint.set(align='center',height=2)
        eprint.text(printline(_('         TOTAL'),money(receipt['total_with_tax']),width=40, ratio=0.6))
        eprint.text('\n\n');
        
        # Paymentlines
        eprint.set(align='center')
        for line in receipt['paymentlines']:
            eprint.text(printline(line['journal'], money(line['amount']), ratio=0.6))

        eprint.text('\n');
        eprint.set(align='center',height=2)
        eprint.text(printline(_('        CHANGE'),money(receipt['change']),width=40, ratio=0.6))
        eprint.set(align='center')
        eprint.text('\n');

        # Extra Payment info
        if receipt['total_discount'] != 0:
            eprint.text(printline(_('Discounts'),money(receipt['total_discount']),width=40, ratio=0.6))
        if taxincluded:
            print_taxes()
            #eprint.text(printline(_('Taxes'),money(receipt['total_tax']),width=40, ratio=0.6))

        # Footer
        if check(receipt['footer']):
            eprint.text('\n'+receipt['footer']+'\n\n')
        eprint.text(receipt['name']+'\n')
        eprint.text(      str(receipt['date']['date']).zfill(2)
                    +'/'+ str(receipt['date']['month']+1).zfill(2)
                    +'/'+ str(receipt['date']['year']).zfill(4)
                    +' '+ str(receipt['date']['hour']).zfill(2)
                    +':'+ str(receipt['date']['minute']).zfill(2) )


driver = EscposDriver()
driver.push_task('printstatus')
drivers['escpos'] = driver


class CustomerDisplayDriver(Thread):
    def __init__(self):
        Thread.__init__(self)
        self.queue = Queue()
        self.lock = Lock()
        self.status = {'status': 'connecting', 'messages': []}
        self.device_name = config.get(
            'customer_display_device_name', 'COM2')
        self.device_rate = int(config.get(
            'customer_display_device_rate', 9600))
        self.device_timeout = int(config.get(
            'customer_display_device_timeout', 2))
        self.serial = False

    def get_status(self):
        self.push_task('status')
        return self.status

    def set_status(self, status, message=None):
        if status == self.status['status']:
            if message is not None and message != self.status['messages'][-1]:
                self.status['messages'].append(message)
        else:
            self.status['status'] = status
            if message:
                self.status['messages'] = [message]
            else:
                self.status['messages'] = []

        if status == 'error' and message:
            _logger.error('Display Error: '+message)
        elif status == 'disconnected' and message:
            _logger.warning('Disconnected Display: '+message)

    def lockedstart(self):
        with self.lock:
            if not self.isAlive():
                self.daemon = True
                self.start()

    def push_task(self, task, data=None):
        self.lockedstart()
        self.queue.put((time.time(), task, data))

    def move_cursor(self, col, row):
        # Bixolon spec : 11. "Move Cursor to Specified Position"
        self.cmd_serial_write('\x1B\x6C' + chr(col) + chr(row))

    def display_text(self, lines):
        _logger.debug(
            "Preparing to send the following lines to LCD: %s" % lines)
        # We don't check the number of rows/cols here, because it has already
        # been checked in the POS client in the JS code
        lines_ascii = []
        for line in lines:
            lines_ascii.append(unidecode(line))
        row = 0
        for dline in lines_ascii:
            row += 1
            self.move_cursor(1, row)
            self.serial_write(dline)

    def setup_customer_display(self):
        '''Set LCD cursor to off
        If your LCD has different setup instruction(s), you should
        inherit this function'''
        # Bixolon spec : 35. "Set Cursor On/Off"
        self.cmd_serial_write('\x1F\x43\x00')
        _logger.debug('LCD cursor set to off')

    def clear_customer_display(self):
        '''If your LCD has different clearing instruction, you should inherit
        this function'''
        # Bixolon spec : 12. "Clear Display Screen and Clear String Mode"
        self.cmd_serial_write('\x0C')
        _logger.debug('Customer display cleared')

    def cmd_serial_write(self, command):
        '''If your LCD requires a prefix and/or suffix on all commands,
        you should inherit this function'''
        assert isinstance(command, str), 'command must be a string'
        self.serial_write(command)

    def serial_write(self, text):
        assert isinstance(text, str), 'text must be a string'
        self.serial.write(text)

    def send_text_customer_display(self, text_to_display):
        '''This function sends the data to the serial/usb port.
        We open and close the serial connection on every message display.
        Why ?
        1. Because it is not a problem for the customer display
        2. Because it is not a problem for performance, according to my tests
        3. Because it allows recovery on errors : you can unplug/replug the
        customer display and it will work again on the next message without
        problem
        '''
        lines = simplejson.loads(text_to_display)
        assert isinstance(lines, list), 'lines_list should be a list'
        try:
            _logger.debug(
                'Opening serial port %s for customer display with baudrate %d'
                % (self.device_name, self.device_rate))
            self.serial = Serial(
                self.device_name, self.device_rate,
                timeout=self.device_timeout)
            _logger.debug('serial.is_open = %s' % self.serial.isOpen())
            self.setup_customer_display()
            self.clear_customer_display()
            self.display_text(lines)
        except Exception, e:
            _logger.error('Exception in serial connection: %s' % str(e))
        finally:
            if self.serial:
                _logger.debug('Closing serial port for customer display')
                self.serial.close()

    def run(self):
        while True:
            try:
                timestamp, task, data = self.queue.get(True)
                if task == 'display':
                    self.send_text_customer_display(data)
                elif task == 'status':
                    pass
            except Exception as e:
                self.set_status('error', str(e))

display_driver = CustomerDisplayDriver()
drivers['customer_display'] = display_driver


class Scanner(Thread):
    def __init__(self):
        Thread.__init__(self)
        self.lock = Lock()
        self.status = {'status':'connecting', 'messages':[]}
        self.input_dir = '/dev/input/by-id/'
        self.barcodes = Queue()
        self.keymap = {
            2: ("1","!"),
            3: ("2","@"),
            4: ("3","#"),
            5: ("4","$"),
            6: ("5","%"),
            7: ("6","^"),
            8: ("7","&"),
            9: ("8","*"),
            10:("9","("),
            11:("0",")"),
            12:("-","_"),
            13:("=","+"),
            # 14 BACKSPACE
            # 15 TAB
            16:("q","Q"),
            17:("w","W"),
            18:("e","E"),
            19:("r","R"),
            20:("t","T"),
            21:("y","Y"),
            22:("u","U"),
            23:("i","I"),
            24:("o","O"),
            25:("p","P"),
            26:("[","{"),
            27:("]","}"),
            # 28 ENTER
            # 29 LEFT_CTRL
            30:("a","A"),
            31:("s","S"),
            32:("d","D"),
            33:("f","F"),
            34:("g","G"),
            35:("h","H"),
            36:("j","J"),
            37:("k","K"),
            38:("l","L"),
            39:(";",":"),
            40:("'","\""),
            41:("`","~"),
            # 42 LEFT SHIFT
            43:("\\","|"),
            44:("z","Z"),
            45:("x","X"),
            46:("c","C"),
            47:("v","V"),
            48:("b","B"),
            49:("n","N"),
            50:("m","M"),
            51:(",","<"),
            52:(".",">"),
            53:("/","?"),
            # 54 RIGHT SHIFT
            57:(" "," "),
        }

    def lockedstart(self):
        with self.lock:
            if not self.isAlive():
                self.daemon = True
                self.start()

    def set_status(self, status, message = None):
        if status == self.status['status']:
            if message != None and message != self.status['messages'][-1]:
                self.status['messages'].append(message)
        else:
            self.status['status'] = status
            if message:
                self.status['messages'] = [message]
            else:
                self.status['messages'] = []

        if status == 'error' and message:
            _logger.error('Barcode Scanner Error: '+message)
        elif status == 'disconnected' and message:
            _logger.info('Disconnected Barcode Scanner: %s', message)

    def get_device(self):
        try:
            devices   = [ device for device in listdir(self.input_dir)]
            keyboards = [ device for device in devices if ('kbd' in device) and ('keyboard' not in device.lower())]
            scanners  = [ device for device in devices if ('barcode' in device.lower()) or ('scanner' in device.lower())]
            if len(scanners) > 0:
                self.set_status('connected','Connected to '+scanners[0])
                return evdev.InputDevice(join(self.input_dir,scanners[0]))
            elif len(keyboards) > 0:
                self.set_status('connected','Connected to '+keyboards[0])
                return evdev.InputDevice(join(self.input_dir,keyboards[0]))
            else:
                self.set_status('disconnected','Barcode Scanner Not Found')
                return None
        except Exception as e:
            self.set_status('error',str(e))
            return None

    def get_barcode(self):
        """ Returns a scanned barcode. Will wait at most 5 seconds to get a barcode, and will
            return barcode scanned in the past if they are not older than 5 seconds and have not
            been returned before. This is necessary to catch barcodes scanned while the POS is
            busy reading another barcode
        """
        self.lockedstart()

        while True:
            try:
                timestamp, barcode = self.barcodes.get(True, 5)
                if timestamp > time.time() - 5:
                    return barcode
            except Empty:
                return ''

    def get_status(self):
        self.lockedstart()
        return self.status

    def run(self):
        """ This will start a loop that catches all keyboard events, parse barcode
            sequences and put them on a timestamped queue that can be consumed by
            the point of sale's requests for barcode events
        """

        self.barcodes = Queue()

        barcode  = []
        shift    = False
        device   = None

        while True: # barcodes loop
            if device:  # ungrab device between barcodes and timeouts for plug & play
                try:
                    device.ungrab()
                except Exception as e:
                    self.set_status('error',str(e))
            else:
                time.sleep(5)   # wait until a suitable device is plugged
                device = self.get_device()

            try:
                device.grab()
                shift = False
                barcode = []

                while True: # keycode loop
                    r,w,x = select([device],[],[],5)
                    if len(r) == 0: # timeout
                        break
                    events = device.read()

                    for event in events:
                        if event.type == evdev.ecodes.EV_KEY:
                            #_logger.debug('Evdev Keyboard event %s',evdev.categorize(event))
                            if event.value == 1: # keydown events
                                if event.code in self.keymap:
                                    if shift:
                                        barcode.append(self.keymap[event.code][1])
                                    else:
                                        barcode.append(self.keymap[event.code][0])
                                elif event.code == 42 or event.code == 54: # SHIFT
                                    shift = True
                                elif event.code == 28: # ENTER, end of barcode
                                    self.barcodes.put( (time.time(),''.join(barcode)) )
                                    barcode = []
                            elif event.value == 0: #keyup events
                                if event.code == 42 or event.code == 54: # LEFT SHIFT
                                    shift = False

            except Exception as e:
                self.set_status('error',str(e))

scanner_thread = None
if evdev:
    scanner_thread = Scanner()
    drivers['scanner'] = scanner_thread


class Proxy(http.Controller):

    def get_status(self):
        statuses = {}
        for driver in drivers:
            statuses[driver] = drivers[driver].get_status()
        return statuses

    @http.route('/pos/hello', type='http', auth='none', cors='*')
    def hello(self):
        return "ping"

    @http.route('/pos/handshake', type='json', auth='none', cors='*')
    def handshake(self):
        return True

    @http.route('/pos/status', type='http', auth='none', cors='*')
    def status_http(self):
        resp = """
<!DOCTYPE HTML>
<html>
    <head>
        <title>Odoo's PosBox</title>
        <style>
        body {
            width: 480px;
            margin: 60px auto;
            font-family: sans-serif;
            text-align: justify;
            color: #6B6B6B;
        }
        .device {
            border-bottom: solid 1px rgb(216,216,216);
            padding: 9px;
        }
        .device:nth-child(2n) {
            background:rgb(240,240,240);
        }
        </style>
    </head>
    <body>
        <h1>Hardware Status</h1>
        <p>The list of enabled drivers and their status</p>
"""
        statuses = self.get_status()
        for driver in statuses:

            status = statuses[driver]

            # status['status']= 'connected' # My hack to make the device connected to my system

            if status['status'] == 'connecting':
                color = 'black'
            elif status['status'] == 'connected':
                color = 'green'
            else:
                color = 'red'

            resp += "<h3 style='color:"+color+";'>"+driver+' : '+status['status']+"</h3>\n"
            resp += "<ul>\n"
            for msg in status['messages']:
                resp += '<li>'+msg+'</li>\n'
            resp += "</ul>\n"
        resp += """
            <h2>Connected Devices</h2>
            <p>The list of connected USB devices as seen by the posbox</p>
        """
        devices = commands.getoutput("lsusb").split('\n')
        resp += "<div class='devices'>\n"
        for device in devices:
            device_name = device[device.find('ID')+2:]
            resp+= "<div class='device' data-device='"+device+"'>"+device_name+"</div>\n"
        resp += "</div>\n"
        resp += """
            <h2>Add New Printer</h2>
            <p>
            Copy and paste your printer's device description in the form below. You can find
            your printer's description in the device list above. If you find that your printer works
            well, please send your printer's description to <a href='mailto:support@odoo.com'>
            support@openerp.com</a> so that we can add it to the default list of supported devices.
            </p>
            <form action='/hw_proxy/escpos/add_supported_device' method='GET'>
                <input type='text' style='width:400px' name='device_string' placeholder='123a:b456 Sample Device description' />
                <input type='submit' value='submit' />
            </form>
            <h2>Reset To Defaults</h2>
            <p>If the added devices cause problems, you can <a href='/hw_proxy/escpos/reset_supported_devices'>Reset the
            device list to factory default.</a> This operation cannot be undone.</p>
        """
        resp += "</body>\n</html>\n\n"

        return request.make_response(resp,{
            'Cache-Control': 'no-cache',
            'Content-Type': 'text/html; charset=utf-8',
            'Access-Control-Allow-Origin':  '*',
            'Access-Control-Allow-Methods': 'GET',
            })


    @http.route('/pos/scan_item_success', type='json', auth='none', cors='*')
    def scan_item_success(self, ean):
        """
        A product has been scanned with success
        """
        print 'scan_item_success: ' + str(ean)

    @http.route('/pos/scan_item_error_unrecognized', type='json', auth='none', cors='*')
    def scan_item_error_unrecognized(self, ean):
        """
        A product has been scanned without success
        """
        print 'scan_item_error_unrecognized: ' + str(ean)

    @http.route('/pos/help_needed', type='json', auth='none', cors='*')
    def help_needed(self):
        """
        The user wants an help (ex: light is on)
        """
        print "help_needed"

    @http.route('/pos/help_canceled', type='json', auth='none', cors='*')
    def help_canceled(self):
        """
        The user stops the help request
        """
        print "help_canceled"

    @http.route('/pos/payment_request', type='json', auth='none', cors='*')
    def payment_request(self, price):
        """
        The PoS will activate the method payment
        """
        print "payment_request: price:"+str(price)
        return 'ok'

    @http.route('/pos/payment_status', type='json', auth='none', cors='*')
    def payment_status(self):
        print "payment_status"
        return { 'status':'waiting' }

    @http.route('/pos/payment_cancel', type='json', auth='none', cors='*')
    def payment_cancel(self):
        print "payment_cancel"

    @http.route('/pos/transaction_start', type='json', auth='none', cors='*')
    def transaction_start(self):
        print 'transaction_start'

    @http.route('/pos/transaction_end', type='json', auth='none', cors='*')
    def transaction_end(self):
        print 'transaction_end'

    @http.route('/pos/cashier_mode_activated', type='json', auth='none', cors='*')
    def cashier_mode_activated(self):
        print 'cashier_mode_activated'

    @http.route('/pos/cashier_mode_deactivated', type='json', auth='none', cors='*')
    def cashier_mode_deactivated(self):
        print 'cashier_mode_deactivated'

    @http.route('/pos/open_cashbox', type='json', auth='none', cors='*')
    def open_cashbox(self):
        print 'open_cashbox'

    @http.route('/pos/print_receipt', type='json', auth='none', cors='*')
    def print_receipt(self, receipt):
        print 'print_receipt' + str(receipt)

    @http.route('/pos/is_scanner_connected', type='json', auth='none', cors='*')
    def is_scanner_connected(self, receipt):
        print 'is_scanner_connected?'
        return False

    @http.route('/pos/scanner', type='json', auth='none', cors='*')
    def scanner(self, receipt):
        print 'scanner'
        time.sleep(10)
        return ''

    @http.route('/pos/log', type='http', auth='none', cors='*')
    def log(self, arguments):
        _logger.info(' '.join(str(v) for v in arguments))

    @http.route('/http/print_pdf_invoice', type='http', auth='none', cors='*')
    def print_pdf_invoice(self, pdfinvoice):
        print 'print_pdf_invoice' + str(pdfinvoice)


class EscposProxy(Proxy):
    
    @http.route('/pos/open_cashbox', type='json', auth='none', cors='*')
    def open_cashbox(self):
        _logger.info('ESC/POS: OPEN CASHBOX')
        driver.push_task('cashbox')

    @http.route('/pos/print_receipt', type='json', auth='none', cors='*')
    def print_receipt(self, receipt):
        _logger.info('ESC/POS: PRINT RECEIPT')
        driver.push_task('receipt',receipt)

    @http.route('/pos/print_xml_receipt', type='json', auth='none', cors='*')
    def print_xml_receipt(self, receipt):
        _logger.info('ESC/POS: PRINT XML RECEIPT') 
        driver.push_task('xml_receipt',receipt)

    @http.route('/pos/escpos/add_supported_device', type='json', auth='none', cors='*')
    def add_supported_device(self, device_string):
        _logger.info('ESC/POS: ADDED NEW DEVICE:'+device_string)
        driver.add_supported_device(device_string)
        return "The device:\n"+device_string+"\n has been added to the list of supported devices.<br/><a href='/pos/status'>Ok</a>"

    @http.route('/pos/escpos/reset_supported_devices', type='json', auth='none', cors='*')
    def reset_supported_devices(self):
        try:
            os.remove('escpos_devices.pickle')
        except Exception as e:
            pass
        return 'The list of supported devices has been reset to factory defaults.<br/><a href="/pos/status">Ok</a>'

    @http.route('/pos/send_text_customer_display', type='json', auth='none', cors='*')
    def send_text_customer_display(self, text_to_display):
        _logger.info('LCD: Call send_text_customer_display with text=%s', text_to_display)
        display_driver.push_task('display', text_to_display)

    @http.route('/pos/scanner', type='json', auth='none', cors='*')
    def scanner(self):
        return scanner_thread.get_barcode() if scanner_thread else None