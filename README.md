# GTNH Material Cost Calculator

vibecoded garbage that lets you input the items you want to craft and the recipes you want to use for each intermediate step, resulting in a raw materials list and recipe order graph. recipes extracted using [ShadowTheAge's fork of nesql-exporter](https://github.com/ShadowTheAge/nesql-exporter/).

## running it

needs python 3.12+ (for `lzma`), plus flask and pillow.

```
pip install flask pillow
python run.py
```

opens http://127.0.0.1:5057/ in a browser. `--port 8080` if you want.

the first start takes about half a minute because uncompression
