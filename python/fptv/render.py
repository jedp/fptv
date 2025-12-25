from __future__ import annotations

from typing import List

import pygame

from fptv.tvh import Channel

FG_NORM = (220, 220, 220)
FG_SEL = (0, 0, 0)
BG_NORM = (0, 0, 0)
BG_SEL = (90, 105, 255)
FG_INACT = (180, 180, 180)
FG_ACT = (0, 0, 0)
BG_INACT = (0, 0, 0)
BG_ACT = (90, 105, 255)
FG_ALERT = (255, 40, 40)
FG_ACCENT_BLUE = (90, 105, 255)
FG_ACCENT_YELLOW = (220, 150, 0)


def draw_menu(surface, title_font, item_font,
              items: List[str], selected: int):
    surface.fill((0, 0, 0))
    text_fp = title_font.render("FP", True, FG_ACCENT_YELLOW)
    text_tv = title_font.render("TV", True, FG_ACCENT_BLUE)
    x, y = 60, 60
    surface.blit(text_fp, (x, y))
    surface.blit(text_tv, (x + text_fp.get_width(), y))

    start_y = 200
    line_h = 70
    line_w = surface.get_width()

    for i, text in enumerate(items):
        is_sel = (i == selected)
        bg_color = BG_SEL if is_sel else BG_NORM
        fg_color = FG_SEL if is_sel else FG_NORM

        y = start_y + i * line_h
        rect = pygame.Rect(x, y, line_w, line_h)
        pygame.draw.rect(surface, bg_color, rect)

        text_surf = item_font.render(text, True, fg_color)
        text_rect = text_surf.get_rect()
        text_rect.midleft = (x, y + line_h // 2)

        surface.blit(text_surf, text_rect)


def draw_browse(surface, item_font,
                channels: List[Channel], selected: int):
    surface.fill(BG_NORM)
    header = "Back"
    fg_color = BG_SEL if selected == -1 else FG_NORM
    bg_color = BG_NORM

    img = item_font.render(header, True, fg_color, bg_color)
    surface.blit(img, (20, 0))

    if not channels:
        draw_centered_text(
            surface, item_font, "No channels", surface.get_height() // 2,
            color=FG_ALERT)
        return

    # Show a window around selection
    h = surface.get_height()
    visible = max(5, (h - 148) // 52)
    half = visible // 2
    start = max(0, selected - half)
    end = min(len(channels), start + visible)
    start = max(0, end - visible)

    y0 = 130
    line_h = 52
    line_w = surface.get_width()
    for row, idx in enumerate(range(start, end)):
        text = channels[idx].name
        is_sel = (idx == selected)
        prefix = "  "
        fg_color = FG_SEL if is_sel else FG_NORM
        bg_color = BG_SEL if is_sel else BG_NORM
        y = y0 + row * line_h
        rect = pygame.Rect(0, y, line_w, line_h)
        pygame.draw.rect(surface, bg_color, rect)
        text_surf = item_font.render(text, True, fg_color)
        text_rect = text_surf.get_rect()
        text_rect.midleft = (20, y + line_h // 2)
        surface.blit(text_surf, text_rect)


def draw_playing(surface, title_font, item_font, small_font, name: str):
    surface.fill((0, 0, 0))
    img = small_font.render("Press to stop and return", True, FG_NORM)
    surface.blit(img, (20, 20))
    draw_centered_text(surface, title_font, "Playing", 90)
    draw_centered_text(surface, item_font, name, 190)


def draw_escaping(surface, large_font, small_font):
    surface.fill((0, 0, 0))
    text_title = large_font.render("Escape the Package!", True, FG_ALERT)
    surface.blit(text_title, (20, 20))
    msg = small_font.render("If you're confused, press the power button.", True, FG_NORM)
    surface.blit(msg, (20, 100))


def draw_centered_text(surface, font, text, y, color=FG_NORM):
    img = font.render(text, True, color)
    r = img.get_rect(center=(surface.get_width() // 2, y))
    surface.blit(img, r)
