# Recording the demo GIF

The README reserves a spot for an animated GIF of `make demo`. Record it
with [vhs](https://github.com/charmbracelet/vhs) (deterministic, scriptable):

```bash
brew install vhs
vhs docs/demo.tape          # writes docs/demo.gif
```

Then drop the image into the README where the placeholder comment sits:

```markdown
![labwire closed-loop demo](docs/demo.gif)
```

Alternative: [asciinema](https://asciinema.org) + `agg` for a lighter file,
or a plain screen recording trimmed to ~30 s. Keep it under 3 MB so the
README loads fast.
