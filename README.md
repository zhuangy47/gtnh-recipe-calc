# GTNH Material Cost Calculator

vibecoded garbage that lets you input the items you want to craft and the recipes you want to use for each intermediate step, resulting in a raw materials list and recipe order graph. recipes extracted using [ShadowTheAge's fork of nesql-exporter](https://github.com/ShadowTheAge/nesql-exporter/).

## running it

needs python 3.12+ (for `lzma`), plus flask and pillow.

```
pip install flask pillow
python run.py
```

opens http://127.0.0.1:5057/ in a browser. `--port 8080` and `--no-browser` if
you want them.

the first start takes about half a minute. github won't take a file over 100 MB
and the recipe dump is 350 MB, so it ships as a 26 MB `.xz` and gets unpacked
and converted into `data/nesql.sqlite` (483 MB) on first run; the icon archive
unpacks the same way. after that it starts in a couple of seconds. both are
build products and both are gitignored, so if either gets corrupted you can
delete `data/nesql.sqlite` or `data/image.zip` and the next start rebuilds it.
