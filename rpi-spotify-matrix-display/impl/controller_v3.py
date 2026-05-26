import os, inspect, sys, math, time, configparser, argparse, warnings
from PIL import Image

from apps_v2 import spotify_player
from modules import spotify_module


def main():
    canvas_width = 64
    canvas_height = 64

    # get arguments
    parser = argparse.ArgumentParser(
                    prog = 'RpiSpotifyMatrixDisplay',
                    description = 'Displays album art of currently playing song on an LED matrix')

    parser.add_argument('-f', '--fullscreen', action='store_true', help='Always display album art in fullscreen')
    parser.add_argument('-e', '--emulated', action='store_true', help='Run in a matrix emulator')
    args = parser.parse_args()

    is_emulated = args.emulated
    is_full_screen_always = args.fullscreen

    # switch matrix library import if emulated
    if is_emulated:
        from RGBMatrixEmulator import RGBMatrix, RGBMatrixOptions
    else:
        from rgbmatrix import RGBMatrix, RGBMatrixOptions

    # get config
    currentdir = os.path.dirname(os.path.abspath(inspect.getfile(inspect.currentframe())))
    sys.path.append(currentdir+"/rpi-rgb-led-matrix/bindings/python")

    config = configparser.ConfigParser()
    parsed_configs = config.read('../config.ini')

    if len(parsed_configs) == 0:
        print("no config file found")
        sys.exit()

    # connect to Spotify and create display image
    modules = { 'spotify' : spotify_module.SpotifyModule(config) }
    app_list = [ spotify_player.SpotifyScreen(config, modules, is_full_screen_always) ]

    # setup matrix
    options = RGBMatrixOptions()
    options.hardware_mapping = config.get('Matrix', 'hardware_mapping', fallback='regular')
    options.rows = canvas_width
    options.cols = canvas_height
    normal_brightness = 100 if is_emulated else config.getint('Matrix', 'brightness', fallback=100)
    idle_brightness   = 100 if is_emulated else config.getint('Matrix', 'idle_brightness', fallback=20)
    options.brightness = normal_brightness
    options.gpio_slowdown = config.getint('Matrix', 'gpio_slowdown', fallback=1)
    options.limit_refresh_rate_hz = config.getint('Matrix', 'limit_refresh_rate_hz', fallback=0)
    options.pixel_mapper_config = config.get('Matrix', 'pixel_mapper_config', fallback='')
    options.drop_privileges = False
    matrix = RGBMatrix(options = options)

    shutdown_delay = config.getint('Matrix', 'shutdown_delay', fallback=600)
    black_screen = Image.new("RGB", (canvas_width, canvas_height), (0,0,0))
    last_active_time = math.floor(time.time())
    current_brightness = normal_brightness  # track to avoid redundant writes

    # generate image
    while(True):
        try:
            result = app_list[0].generate()
            # generate() returns (frame, is_playing) or (frame, is_playing, is_idle_active)
            if isinstance(result, tuple) and len(result) == 3:
                frame, is_playing, is_idle_active = result
            elif isinstance(result, tuple) and len(result) == 2:
                frame, is_playing = result
                is_idle_active = False
            else:
                frame, is_playing, is_idle_active = None, False, False
        except Exception as e:
            print(f"[spotify] generate error: {e}", flush=True)
            frame, is_playing, is_idle_active = None, False, False
        current_time = math.floor(time.time())

        if frame is not None:
            if is_playing:
                last_active_time = math.floor(time.time())
            elif current_time - last_active_time >= shutdown_delay:
                frame = black_screen
        else:
            frame = black_screen

        # Apply brightness: idle brightness only when the fallback content is shown
        target_brightness = idle_brightness if is_idle_active else normal_brightness
        if target_brightness != current_brightness:
            print(f"[ctrl] brightness {current_brightness} → {target_brightness} (is_idle_active={is_idle_active})", flush=True)
            matrix.brightness = target_brightness
            current_brightness = target_brightness

        matrix.SetImage(frame)
        time.sleep(0.08)


if __name__ == '__main__':
    try:
        warnings.filterwarnings("ignore", category=DeprecationWarning)
        main()
    except KeyboardInterrupt:
        print('Interrupted with Ctrl-C')
        sys.exit(0)
