from pathlib import Path

path = Path(r"C:\BOT-ATRI\BOT-ATRI-main\tools\launch\watchdog.ps1")
text = path.read_text(encoding="utf-8-sig")
path.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
print("BOM ensured, size:", len(text), "chars")
