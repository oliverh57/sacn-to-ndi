# sACN to NDI

Turns a pixel matrix controlled by a lighting desk over **sACN (E1.31)** into an
**NDI video source**, so it can be used in Depence, Resolume, OBS, vMix or anything
else that takes NDI.

The default setup is a 10 × 10 RGB matrix on universe 1, address 1 (channels 1–300).
It comes out as the NDI source `<YOUR-MAC>.LOCAL (sACN Matrix)` at 320 × 320, 50 fps,
with each LED pixel drawn as a 32 × 32 block.

## Requirements

- Python 3.9 or newer. Only the standard library is used; there's nothing to `pip install`.
- The NDI runtime:
  - **macOS:** install [NDI Tools](https://ndi.video/tools/), which provides `/usr/local/lib/libndi.dylib`.
  - **Windows:** install the NDI Runtime or NDI Tools, which provides `Processing.NDI.Lib.x64.dll`.
  - **Linux:** `libndi.so`.

  To use a different library, set `NDI_LIB=/path/to/libndi`.

## Run

On macOS, double-click **`Start sACN to NDI.command`**. It stops any copy that's
already running, then starts the app. Or run it from a terminal:

```bash
python3 sacn2ndi.py
```

A settings page opens at <http://localhost:8090> with:

- **sACN input:** network interface (all, localhost or a specific port), universe,
  start address, multicast on/off, and hold the last look or black out when the desk stops
- **Matrix:** width × height, colour mode (RGB, RBG, GRB, BGR, RGBW, W), whether the
  patch runs along rows or columns, which corner pixel 1 is in, snake/zig-zag wiring,
  pixels per universe
- **NDI output:** source name, video pixels per LED, gap between LEDs, frame rate, master
- A **live preview** of the matrix, the desk that's sending, packet rate and NDI receiver count

Changes take effect immediately and are saved to `settings.json`. Any setting can also
be given on the command line, which overrides the saved value. Run
`python3 sacn2ndi.py --help` to see them all.

## Receiving sACN

- Multicast is received on every interface, including localhost, unless you pick one interface.
- Unicast sent to this machine's IP or to `127.0.0.1` is always received.
- When several sources send the same universe, the highest priority wins. Sources with
  equal priority are merged HTP (highest value wins).
- A source that sends "stream terminated" drops out immediately. A source that goes
  silent drops out after 2.5 s.
- Large matrices spill over onto the next universes automatically.

## Testing without a desk

```bash
python3 sacn_test_sender.py --multicast                         # moving rainbow
python3 sacn_test_sender.py --multicast --pattern chase         # lights pixels in patch order
python3 sacn_test_sender.py --multicast --interface 127.0.0.1   # localhost only
```

If two programs on the same Mac are listening for sACN, macOS gives unicast packets to
only one of them, so use `--multicast` when testing.

## Notes

- On a Mac with no HostName set, the NDI runtime names the machine after whatever the
  router hands out (for example `MAC.LAN`). To avoid that, the app writes a small NDI
  config into `.ndi/` that sets the machine name to the Mac's network name. To choose
  your own name, use `--machine-name`.
- The settings page only listens on `localhost`.
