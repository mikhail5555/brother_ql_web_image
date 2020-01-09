#!/usr/bin/env python

"""
This is a web service to print labels on Brother QL label printers.
"""

import sys, logging, random, json, argparse, os
from io import BytesIO

from bottle import run, route, get, post, response, request, jinja2_view as view, static_file, redirect
from PIL import Image

from brother_ql.devicedependent import models, label_type_specs, label_sizes
from brother_ql.devicedependent import ENDLESS_LABEL, DIE_CUT_LABEL, ROUND_DIE_CUT_LABEL
from brother_ql import BrotherQLRaster, create_label
from brother_ql.backends import backend_factory, guess_backend

logger = logging.getLogger(__name__)

LABEL_SIZES = [(name, label_type_specs[name]['name'], label_type_specs[name]['dots_printable']) for name in label_sizes]
IMAGE_EXTENSIONS = ['.png', '.jpg', '.jpeg', '.bmp']

try:
    with open('config.json') as fh:
        CONFIG = json.load(fh)
except FileNotFoundError as e:
    with open('config.example.json') as fh:
        CONFIG = json.load(fh)


@route('/')
def index():
    redirect('/labeldesigner')


@route('/static/<filename:path>')
def serve_static(filename):
    return static_file(filename, root='./static')


@route('/labeldesigner')
@view('labeldesigner.jinja2')
def labeldesigner():
    return {'label_sizes': LABEL_SIZES,
            'files': FILE_NAMES,
            'website': CONFIG['WEBSITE'],
            'label': CONFIG['LABEL']}


@route('/upload', method='POST')
def do_upload():
    upload = request.files.get('upload')
    name, ext = os.path.splitext(upload.filename)

    if ext not in IMAGE_EXTENSIONS:
        return "File extension not allowed."

    file_path = os.path.join(IMG_DIR, upload.filename)

    upload.save(file_path)
    update_files()

    redirect('/labeldesigner')


def get_label_context(request):
    """ might raise LookupError() """

    d = request.params.decode()  # UTF-8 decoded form data

    context = {
        'file_name': d.get('file_name', "image.png"),
        'label_size': d.get('label_size', "62"),
        'kind': label_type_specs[d.get('label_size', "62")]['kind'],
        'orientation': d.get('orientation', 'standard'),
    }

    return context


def image_exists(image_name):
    update_files()
    return image_name in FILE_NAMES


@get('/api/preview/image')
@post('/api/preview/image')
def get_preview_image():
    try:
        context = get_label_context(request)
    except LookupError as e:
        return e.msg

    if not image_exists(context["file_name"]):
        logger.warning('Exception happened: Image not in directory')
        return "Image not in directory"

    im = Image.open(os.path.join(IMG_DIR, context["file_name"]))

    if context['orientation'] == 'rotated+90':
        im = im.transpose(Image.ROTATE_90)
    elif context['orientation'] == 'rotated-90':
        im = im.transpose(Image.ROTATE_270)

    return_format = request.query.get('return_format', 'png')
    if return_format == 'base64':
        import base64
        response.set_header('Content-type', 'text/plain')
        return base64.b64encode(image_to_png_bytes(im))
    else:
        response.set_header('Content-type', 'image/png')
        return image_to_png_bytes(im)


@get('/api/delete/image')
@post('/api/delete/image')
def delete_image():
    try:
        context = get_label_context(request)
    except LookupError as e:
        return e.msg

    if not image_exists(context["file_name"]):
        logger.warning('Exception happened: Image not in directory')
        return "Image not in directory"

    image = IMG_DIR + context["file_name"]

    os.remove(image)
    update_files()

    redirect('/labeldesigner')


def update_files():
    global FILE_NAMES
    FILE_NAMES = [f for f in os.listdir(IMG_DIR) if (os.path.isfile(os.path.join(IMG_DIR, f)) and os.path.splitext(f)[1] in IMAGE_EXTENSIONS)]


def image_to_png_bytes(im):
    image_buffer = BytesIO()
    im.save(image_buffer, format="PNG")
    image_buffer.seek(0)
    return image_buffer.read()


@post('/api/print/image')
@get('/api/print/image')
def print_image():
    """
    API to print a label

    returns: JSON

    Ideas for additional URL parameters:
    - alignment
    """

    return_dict = {'success': False}

    try:
        context = get_label_context(request)
    except LookupError as e:
        return_dict['error'] = e.msg
        return return_dict

    if not image_exists(context["file_name"]):
        return_dict['message'] = "Image not in directory"
        logger.warning('Exception happened: Image not in directory')
        return return_dict

    im = Image.open(os.path.join(IMG_DIR, context["file_name"]))

    if context['kind'] == ENDLESS_LABEL:
        rotate = 0 if context['orientation'] == 'standard' else 90
    elif context['kind'] in (ROUND_DIE_CUT_LABEL, DIE_CUT_LABEL):
        rotate = 'auto'

    qlr = BrotherQLRaster(CONFIG['PRINTER']['MODEL'])
    create_label(qlr, im, context['label_size'], cut=True, rotate=rotate)

    if not DEBUG:
        try:
            be = BACKEND_CLASS(CONFIG['PRINTER']['PRINTER'])
            be.write(qlr.data)
            be.dispose()
            del be
        except Exception as e:
            return_dict['message'] = str(e)
            logger.warning('Exception happened: %s', e)
            return return_dict

    return_dict['success'] = True
    if DEBUG: return_dict['data'] = str(qlr.data)
    return return_dict


def main():
    global DEBUG, BACKEND_CLASS, CONFIG, IMG_DIR, FILE_NAMES

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', default=False)
    parser.add_argument('--loglevel', type=lambda x: getattr(logging, x.upper()), default=False)
    parser.add_argument('--default-label-size', default=False,
                        help='Label size inserted in your printer. Defaults to 62.')
    parser.add_argument('--default-orientation', default=False, choices=('standard', 'rotated'),
                        help='Label orientation, defaults to "standard". To turn your text by 90°, state "rotated".')
    parser.add_argument('--model', default=False, choices=models, help='The model of your printer (default: QL-500)')
    parser.add_argument('printer', nargs='?', default=False,
                        help='String descriptor for the printer to use (like tcp://192.168.0.23:9100 or file:///dev/usb/lp0)')
    args = parser.parse_args()

    IMG_DIR = "./img/"
    update_files()

    if args.printer:
        CONFIG['PRINTER']['PRINTER'] = args.printer

    if args.port:
        PORT = args.port
    else:
        PORT = CONFIG['SERVER']['PORT']

    if args.loglevel:
        LOGLEVEL = args.loglevel
    else:
        LOGLEVEL = CONFIG['SERVER']['LOGLEVEL']

    if LOGLEVEL == 'DEBUG':
        DEBUG = True
    else:
        DEBUG = False

    if args.model:
        CONFIG['PRINTER']['MODEL'] = args.model

    if args.default_label_size:
        CONFIG['LABEL']['DEFAULT_SIZE'] = args.default_label_size

    if args.default_orientation:
        CONFIG['LABEL']['DEFAULT_ORIENTATION'] = args.default_orientation

    logging.basicConfig(level=LOGLEVEL)

    try:
        selected_backend = guess_backend(CONFIG['PRINTER']['PRINTER'])
    except ValueError:
        parser.error("Couln't guess the backend to use from the printer string descriptor")
    BACKEND_CLASS = backend_factory(selected_backend)['backend_class']

    if CONFIG['LABEL']['DEFAULT_SIZE'] not in label_sizes:
        parser.error("Invalid --default-label-size. Please choose on of the following:\n:" + " ".join(label_sizes))

    run(host=CONFIG['SERVER']['HOST'], port=PORT, debug=DEBUG)


if __name__ == "__main__":
    main()
