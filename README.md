# Screenshot Explainer

Drag a box over any part of your screen and Claude explains what is in it — in
a dark panel with an orange border that unrolls and types itself out.

Useful for the thing you are looking at but do not recognise: a stack trace, a
`systemctl status` dump, a chart, a config file, a form in a language you do
not read. You crop the part that matters instead of sending your whole screen,
and you get an explanation rather than a one-line answer.

```
Ctrl+Alt+E   dim the screen, drag out a region, get it explained
Ctrl+Alt+R   explain the same region again, without re-dragging
Ctrl+Alt+H   show/hide the panel
Ctrl+Alt+Q   quit
```

**Windows only.** It leans on Win32 for the transparent overlay, the
click-through panel and layout-proof hotkeys.

## Setup

```powershell
pip install -r requirements.txt
python screen_explainer.py
```

**No API key needed.** By default it uses the Claude Code CLI you are already
signed into, and finds `claude.exe` on its own — including the copy bundled
inside the VS Code extension, whose path changes on every update. If it cannot
find it, pass `--claude-path C:\...\claude.exe`.

To use the Anthropic API directly instead (faster, billed separately):

```powershell
setx ANTHROPIC_API_KEY "sk-ant-..."     # then open a NEW terminal
python screen_explainer.py --backend api
```

Leave the terminal open — it is the running process, and it logs each run.

## Selecting

`Ctrl+Alt+E` dims every monitor and gives you a crosshair. Drag in any
direction; a live `W x H` readout follows the cursor. `Esc`, right-click, or a
click without a drag cancels. The dim layer is torn down before the capture, so
it never ends up in the crop.

`Ctrl+Alt+R` is the one worth remembering: for a log that keeps changing or a
chart that keeps moving, pick the region once and then just hit repeat.

## The panel

- Dark warm background, orange border, orange summary, bullets and **bold**
  terms, soft off-white body text.
- Unrolls downwards over ~220 ms, then types itself out at ~320 chars/sec.
  `Ctrl+Alt+H` once skips the typing; again hides it.
- Sized to its content up to `--max-lines`, and scrolls past that.
- Drag it anywhere by its header. Scroll it with the wheel.
- It never takes keyboard focus, so whatever you were typing in stays focused.

## Options

```
--instruction TEXT      a standing extra instruction, e.g. "answer in Portuguese"
--backend cli|api       cli (default, no key) or api
--claude-path EXE       path to claude.exe, if it is not found automatically
--model claude-opus-5   any Claude model id
--effort low|medium|high|xhigh|max      how hard it thinks (api backend)

--corner br             [t]op/[b]ottom + [l]eft/[c]entre/[r]ight (default: br)
--position X,Y          exact pixel position instead of a corner
--width 520             panel width
--max-lines 26          height cap in lines; scrolls past this
--font-size 10
--margin 16

--accent #D97757        border, summary, bullets, bold terms
--bg #1B1A19            panel background
--fg #D8D4C9            body text
--border 2              accent border thickness
--light                 light theme: dark text on white
--transparent           knock the background out entirely
--opacity 0.97

--animate-ms 220        unroll duration; 0 to appear instantly
--type-cps 320          typewriter speed; 0 to show it all at once

--tint #0C0B0A          colour the screen dims to while selecting
--tint-opacity 0.45
--save-shots DIR        where crops are kept (default: ./screenshots)
--no-save-shots         don't keep them
--hotkey / --hotkey-repeat / --hotkey-toggle / --hotkey-quit
                        a single key (`.`), a code (`vk:190`), or `<ctrl>+<alt>+e`
```

Examples:

```powershell
python screen_explainer.py --instruction "responde sempre em portugues"
python screen_explainer.py --width 680 --font-size 11 --corner tr
python screen_explainer.py --type-cps 0 --animate-ms 0     # no effects
python screen_explainer.py --accent "#7AA2F7" --bg "#16161E"
```

## Notes

- **Hotkeys are matched by virtual-key code, not by character**, so non-US
  layouts work as-is. Avoid `~` and `´` — on a Portuguese keyboard those are
  dead keys that emit no character until you press a second key, so they can
  never fire. Bind by code (`vk:191`) if you really want one.
- Crops are saved to `screenshots/` with a timestamp, which is handy for
  checking an explanation afterwards. The folder grows; `--no-save-shots` turns
  it off and `--save-shots DIR` puts it elsewhere. It is gitignored.
- `--transparent` is mostly a curiosity: the text floats with no background at
  all, which looks good over a plain window and is unreadable over a busy one.
- Each capture is one request to Claude. Nothing is sent until you press the
  hotkey.
