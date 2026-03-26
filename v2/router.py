from fastapi import APIRouter
from fastapi.responses import HTMLResponse

v2_router = APIRouter()


@v2_router.get("/", response_class=HTMLResponse)
def v2_index():
    return HTMLResponse("""<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Prisammenligning V2</title>
  <style>
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial; margin: 32px; color:#1a1a1a; }
    .card { border:1px solid #e6e6e6; padding:24px; border-radius:12px; max-width:600px; }
    a { color:#1F4E79; text-decoration:none; }
    a:hover { text-decoration:underline; }
  </style>
</head>
<body>
  <div class="card">
    <h2>Prisammenligning V2</h2>
    <p>V2-modulen er under utvikling.</p>
    <p><a href="/">&larr; Tilbake til Prisammenligning V1</a></p>
  </div>
</body>
</html>""")
