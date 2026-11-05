# Templates

A template is the look of the badge: the fade at the bottom, the font, its size and position, and which score to show.

## The Templates page

Pick a poster, move the sliders and see the result live. **Save template** with the name box empty updates the template you have open (the built-in Default is saved as your own copy; **Reset to built-in** undoes that). Type a name to make a new template instead; a name that is already taken is refused, and your settings stay as they are. Saved templates can be used for scheduled runs (Settings), per title (overrides), or with `--template NAME`.

**Edit YAML...** opens the template as text. **Check** validates it and draws a test poster; **Check and save** only saves if it passes.

## Score source

**MDBList combined score** is the default. Choose IMDb, TMDb, Trakt, Letterboxd, Rotten Tomatoes, Metacritic or Roger Ebert instead, and titles without that score fall back to the combined score.

The combined score can be dragged far below the sources it is built from when a title lacks several of them (a film whose sources read 68-93 can still come out at 44). MDBList also reports the plain **average** of the sources that have a score; when that is 5 or more points higher than the combined score, laminator uses the average instead. A film whose sources are really low keeps its low score, because the two numbers agree. A source you pick by name (IMDb, Trakt, ...) is always shown as it is.

## The YAML

```yaml
sources: [aggregate]      # first one with a value wins
decimals: 1               # 7.3; 0 for 73
gradient:
  height: 0.12            # share of the poster's height
  color: [0, 0, 0]
  max_opacity: 0.65
score:
  font: Anton-Regular.ttf
  size: 0.0433            # share of the poster's height
  color: [255, 255, 255]
  anchor: bottom-right
  margin_x: 0.055
  margin_y: 0.02
  shadow: true
```

Sizes are fractions of the poster, so it looks the same at any resolution.

## Fonts

Put your own `.ttf` or `.otf` files in `<data>/fonts/`. Bundled fonts (Anton, Barlow Condensed, Bebas Neue, Inter, Montserrat, Outfit, Poppins) are under the SIL Open Font License.
