#!/usr/bin/env python3
import sys
import pygame

"""
Hello World script to test fullscreen pygame
from a headless ssh console.

Usage:

    DISPLAY=:0 ./hello.py

"""

def main():
    pygame.init()

    # Fullscreen window at current desktop resolution
    screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
    pygame.display.set_caption("Hello")

    # Hide mouse cursor
    pygame.mouse.set_visible(False)

    # Basic font (default)
    font = pygame.font.Font(None, 96)

    text = font.render("Hello, World!", True, (255, 255, 255))
    rect = text.get_rect(center=screen.get_rect().center)

    clock = pygame.time.Clock()

    while True:
        # Handle window manager events so X doesn’t think we’re frozen
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                pygame.quit()
                sys.exit(0)

        screen.fill((0, 0, 0))
        screen.blit(text, rect)
        pygame.display.flip()

        clock.tick(30)  # limit CPU usage

if __name__ == "__main__":
    main()

