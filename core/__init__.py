"""Core do RAG: configuração, componentes LangChain, ingestão e CLI."""
import sys

# Windows usa cp1252 no console por padrão; sem isso os prints com emoji quebram.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")
