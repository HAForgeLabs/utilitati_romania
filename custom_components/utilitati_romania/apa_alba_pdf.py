"""Descărcarea securizată a facturilor Apă Alba prin sesiunea integrării."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from aiohttp import web
from homeassistant.components.http import HomeAssistantView

from .const import DOMENIU
from .coordonator import CoordonatorUtilitatiRomania
from .furnizori.apa_alba import ClientFurnizorApaAlba


PDF_ROUTE_PREFIX = "/api/utilitati_romania/apa_alba/factura"
_PORTAL_HOST = "contulmeu.apaalba.ro"


def _este_document_autorizat(link: object, invoice_id: str) -> bool:
    """Acceptă numai adresa PDF a portalului și identificatorul exact al facturii."""
    if not isinstance(link, str) or not link.strip():
        return False
    url = urlsplit(link.strip())
    if url.scheme or url.netloc:
        if url.scheme != "https" or url.hostname != _PORTAL_HOST or url.port not in (None, 443):
            return False
        if url.username or url.password:
            return False
    if url.fragment:
        return False
    return url.path == f"/facturile-mele/{invoice_id}/download"


class VizualizareFacturaApaAlba(HomeAssistantView):
    url = PDF_ROUTE_PREFIX + "/{entry_id}/{invoice_id}"
    name = "api:utilitati_romania:apa_alba:factura"
    requires_auth = True

    def __init__(self, hass) -> None:
        self._hass = hass

    async def get(self, request: web.Request, entry_id: str, invoice_id: str) -> web.Response:
        if not re.fullmatch(r"[0-9]{1,20}", invoice_id):
            raise web.HTTPBadRequest(text="Identificator de factură invalid")

        coordonator = self._hass.data.get(DOMENIU, {}).get(entry_id)
        if not isinstance(coordonator, CoordonatorUtilitatiRomania) or not isinstance(
            coordonator.client, ClientFurnizorApaAlba
        ):
            raise web.HTTPNotFound(text="Cont Apă Alba indisponibil")

        instantaneu = coordonator.data
        if instantaneu is None:
            raise web.HTTPNotFound(text="Nu există încă date de facturare")

        document_permis = any(
            _este_document_autorizat(
                (factura.date_brute or {}).get("pdf_url"), invoice_id
            )
            for factura in instantaneu.facturi
        )
        if not document_permis:
            raise web.HTTPNotFound(text="Factura nu aparține acestui cont")

        try:
            document = await coordonator.client.async_descarca_factura_pdf(invoice_id)
        except Exception as err:
            raise web.HTTPBadGateway(text="Factura nu a putut fi descărcată din portal") from err

        return web.Response(
            body=document,
            content_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="apa_alba_factura_{invoice_id}.pdf"',
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )
