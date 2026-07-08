"""
utils/embeds.py

Shared embed-building helpers used by multiple cogs.
"""
import discord


def add_multi_field(embed: discord.Embed, name: str, lines: list[str], inline: bool = False, empty_text: str = "None"):
    """Adds a list of lines to an embed as a field, splitting across
    multiple fields if the combined text would exceed Discord's 1024
    character-per-field limit (this is what was crashing /furnace status
    and /factory status)."""
    if not lines:
        embed.add_field(name=name, value=empty_text, inline=inline)
        return

    chunk_lines: list[str] = []
    chunk_len = 0
    first = True
    for line in lines:
        added_len = len(line) + (1 if chunk_lines else 0)  # +1 accounts for the newline joining lines
        if chunk_lines and chunk_len + added_len > 1024:
            embed.add_field(name=name if first else f"{name} (cont.)", value="\n".join(chunk_lines), inline=inline)
            first = False
            chunk_lines = [line]
            chunk_len = len(line)
        else:
            chunk_lines.append(line)
            chunk_len += added_len
    if chunk_lines:
        embed.add_field(name=name if first else f"{name} (cont.)", value="\n".join(chunk_lines), inline=inline)