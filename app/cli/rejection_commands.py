"""Records set apart because PostgreSQL jsonb cannot hold them, and their acknowledgement."""

from __future__ import annotations

import json
import sys

import typer

from app.cli._context import context


def rejections(
    limit: int = typer.Option(20, min=1, max=10_000, help="En yeni kaç kayıt listelenir."),
    ack: int | None = typer.Option(
        None,
        "--ack",
        min=1,
        help="Bu numaraya kadar (dahil) reddedilenlere bakıldı; /health bunun için uyarmaz.",
    ),
    element: int | None = typer.Option(
        None, "--element", min=1, help="Bu numaralı kaydın kendisini standart çıktıya yaz."
    ),
) -> None:
    """Eşitlemede reddedilen Google kayıtlarını listele, onayla ya da dışa aktar."""
    from app.services import raw_rejections as service

    if ack is not None and element is not None:
        typer.echo("--ack ve --element birlikte kullanılamaz.", err=True)
        raise typer.Exit(2)
    if element is not None:

        async def load():
            async with context.sessions() as session:
                return await service.load_element(session, element)

        data = context.run(load())
        if data is None:
            typer.echo(f"Reddedilen kayıt bulunamadı: {element}", err=True)
            raise typer.Exit(1)
        sys.stdout.buffer.write(data + b"\n")
        sys.stdout.buffer.flush()
        return
    if ack is not None:
        through = ack

        async def acknowledge():
            async with context.sessions.begin() as session:
                return await service.acknowledge(session, through)

        try:
            current, written = context.run(acknowledge())
        except service.UnknownRejection:
            typer.echo(f"Reddedilen kayıt bulunamadı: {ack}", err=True)
            raise typer.Exit(1) from None
        if written:
            typer.echo(
                f"Onaylandı: {current} numaralı reddedilen kayda kadar (dahil). Yeni bir kayıt "
                "reddedilene kadar /health bu nedenle uyarmaz."
            )
        else:
            typer.echo(f"Zaten onaylı: onay {current} numaralı kayda kadar; yeni onay yazılmadı.")
        return

    async def command():
        async with context.sessions() as session:
            return await service.list_rejections(session, limit=limit)

    typer.echo(json.dumps(context.run(command()), indent=2, ensure_ascii=False))
