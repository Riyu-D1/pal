"""HTML layout. Every page: labelled controls, visible focus, an error
banner slot, and a layout that works at 375px wide."""


def layout(title: str, body: str, req=None, banner=None,
           banner_kind="error", settings=None) -> str:
    authed = req is not None and getattr(req, "session", None)
    nav = ""
    if authed:
        csrf = req.session.get("csrf", "")
        nav = f"""
        <nav aria-label="main">
          <a href="/">Home</a>
          <a href="/approvals">Approvals</a>
          <a href="/memories">Memories</a>
          <a href="/owner-facts">Owner facts</a>
          <form method="post" action="/logout" class="inline">
            <input type="hidden" name="csrf" value="{csrf}">
            <button type="submit" class="link">Sign out</button></form>
        </nav>"""
    ban = (f'<div class="banner {banner_kind}" role="alert">{banner}</div>'
           if banner else "")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} - assistants</title>
<link rel="stylesheet" href="/static/style.css">
</head>
<body>
{nav}
<main>
{ban}
{body}
</main>
<script src="/static/app.js" defer></script>
</body>
</html>"""
