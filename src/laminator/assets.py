"""Where fonts and templates come from. The user's data folder wins over what ships in the package,
so a container only needs /data to customise the look."""
from __future__ import annotations

import os
from pathlib import Path

import yaml

BUNDLED = Path(__file__).parent / "assets"


def font_path(spec: str, data: Path) -> Path:
    """'fonts/Anton-Regular.ttf' or 'Anton-Regular.ttf' -> <data>/fonts, then the bundled fonts.
    An absolute or ~ path is used as is (works on this machine only; not portable to a container)."""
    p = Path(os.path.expanduser(spec))
    if p.is_absolute():
        if p.exists():
            return p
        raise FileNotFoundError(f"font not found: {p}")
    for base in (data / "fonts", BUNDLED / "fonts"):
        if (base / p.name).exists():
            return base / p.name
    raise FileNotFoundError(f"font {spec!r} not found in {data / 'fonts'} or the bundled fonts")


def list_fonts(data: Path) -> list[str]:
    names = {f.name for base in (data / "fonts", BUNDLED / "fonts") if base.is_dir() for f in base.glob("*.[to]tf")}
    return sorted(names)


def template_file(spec: str, data: Path) -> Path:
    """A path to a YAML file, or a template name ('default'); names are looked up in <data>/templates, then bundled."""
    p = Path(spec)
    if p.suffix in (".yaml", ".yml") and p.is_file():
        return p
    for base in (data / "templates", BUNDLED / "templates"):
        for cand in (base / f"{p.stem}.yaml", base / f"{p.stem}.yml"):
            if cand.is_file():
                return cand
    raise FileNotFoundError(f"template {spec!r} not found in {data / 'templates'} or the bundled templates")


def load_template(spec: str, data: Path) -> tuple[dict, str]:
    """(template with the font resolved to a real file, signature of the template as written).
    The signature ignores where the font lives, so moving machines does not re-render everything."""
    import json

    tpl = yaml.safe_load(template_file(spec, data).read_text())
    sig = json.dumps(tpl, sort_keys=True)
    tpl["score"]["font"] = str(font_path(tpl["score"]["font"], data))
    return tpl, sig


# ---- checking a template written or edited as text ------------------------------------------------------------------------
SOURCE_IDS = ["aggregate", "imdb", "tmdb", "trakt", "letterboxd", "tomatoes", "popcorn", "metacritic", "metacriticuser", "rogerebert"]
_TOP = {"sources", "decimals", "gradient", "score"}
_GRAD = {"height": (0.02, 0.4), "max_opacity": (0.0, 1.0)}
_SCORE = {"size": (0.02, 0.12), "margin_x": (0.0, 0.15), "margin_y": (0.0, 0.15), "embolden": (0.0, 0.06)}


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _rgb(v) -> bool:
    return isinstance(v, list) and len(v) == 3 and all(isinstance(x, int) and not isinstance(x, bool) and 0 <= x <= 255 for x in v)


def validate_template_text(text: str, data: Path) -> tuple[dict | None, list[str]]:
    """(template, []) when the text is a usable template, else (None, [sentences saying what to fix]).
    Beyond the YAML syntax this checks every key and range, that the font exists, and that the template really draws."""
    if len(text) > 20000:
        return None, ["the text is too long for a template"]
    try:
        d = yaml.safe_load(text)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f"line {mark.line + 1}: " if mark is not None else ""
        return None, [f"{where}this is not valid YAML ({getattr(e, 'problem', None) or type(e).__name__})"]
    if not isinstance(d, dict):
        return None, ["a template is a set of settings (sources, decimals, gradient, score), not a list or a single value"]
    errs: list[str] = []
    for k in sorted(set(d) - _TOP, key=str):
        errs.append(f"unknown setting {k!r} (allowed: {', '.join(sorted(_TOP))})")
    for k in ("gradient", "score"):
        if not isinstance(d.get(k), dict):
            errs.append(f"{k}: is required and must be a group of settings")
    src = d.get("sources", ["aggregate"])
    if not (isinstance(src, list) and src and all(isinstance(s, str) and s in SOURCE_IDS for s in src)):
        errs.append(f"sources: a non-empty list using only {', '.join(SOURCE_IDS)}")
    if d.get("decimals", 1) not in (0, 1) or isinstance(d.get("decimals", 1), bool):
        errs.append("decimals: 1 (7.3) or 0 (73)")
    g, s = d.get("gradient"), d.get("score")
    if isinstance(g, dict):
        for k in sorted(set(g) - set(_GRAD) - {"color"}, key=str):
            errs.append(f"gradient: unknown setting {k!r}")
        for k, (lo, hi) in _GRAD.items():
            if not (_num(g.get(k)) and lo <= g[k] <= hi):
                errs.append(f"gradient.{k}: a number from {lo} to {hi}")
        if not _rgb(g.get("color")):
            errs.append("gradient.color: three whole numbers 0-255, like [0, 0, 0]")
    if isinstance(s, dict):
        for k in sorted(set(s) - set(_SCORE) - {"font", "color", "anchor", "shadow"}, key=str):
            errs.append(f"score: unknown setting {k!r}")
        for k, (lo, hi) in _SCORE.items():
            if k == "embolden" and k not in s:
                continue
            if not (_num(s.get(k)) and lo <= s[k] <= hi):
                errs.append(f"score.{k}: a number from {lo} to {hi}")
        if not _rgb(s.get("color")):
            errs.append("score.color: three whole numbers 0-255, like [255, 255, 255]")
        if "shadow" in s and not isinstance(s["shadow"], bool):
            errs.append("score.shadow: true or false")
        if s.get("anchor", "bottom-right") != "bottom-right":
            errs.append("score.anchor: only bottom-right is supported")
        font = s.get("font")
        if not isinstance(font, str) or not font:
            errs.append("score.font: the name of a font file")
        elif font not in (Path(font).name, f"fonts/{Path(font).name}") or Path(font).name.startswith("."):
            errs.append("score.font: give just the file name (like Anton-Regular.ttf or fonts/Anton-Regular.ttf), not a path")
        else:
            try:
                font_path(font, data)
            except FileNotFoundError:
                errs.append(f"score.font: {Path(font).name!r} is not one of the available fonts ({', '.join(list_fonts(data))})")
    if errs:
        return None, errs
    try:  # it must actually draw
        import io
        from PIL import Image
        from .render import render
        probe = {**d, "score": {**d["score"], "font": str(font_path(d["score"]["font"], data))}}
        buf = io.BytesIO(); Image.new("RGB", (200, 300), (60, 90, 140)).save(buf, "JPEG")
        render(buf.getvalue(), "7.3", probe)
    except Exception as e:
        return None, [f"the template could not be drawn ({type(e).__name__}: {str(e)[:120]})"]
    return d, []
