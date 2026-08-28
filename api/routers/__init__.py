"""Routers da API — ordem de include = ordem da 1ª rota no app.py original
(paridade de matching preservada; ver docs/arquitetura.md).
"""
from . import auth  # noqa: F401
from . import chat  # noqa: F401
from . import paginas  # noqa: F401
from . import sandbox  # noqa: F401
from . import biblioteca  # noqa: F401
from . import sistema  # noqa: F401
from . import midia  # noqa: F401
from . import telemetria  # noqa: F401
from . import voz  # noqa: F401
from . import provedores  # noqa: F401
from . import agentico  # noqa: F401
from . import jobs  # noqa: F401 — status das famílias (paths únicos)

# ordem de include: 1ª rota de cada router no arquivo original
ORDENADOS = [auth, chat, paginas, sandbox, biblioteca, sistema, midia, telemetria, voz, provedores, agentico, "jobs"]
