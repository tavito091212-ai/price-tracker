"""
Game Pass Scraper — Próximos lanzamientos con fechas y detalles

Scrapia el sitio oficial de Xbox Game Pass para obtener:
- Nombre del juego
- Fecha de lanzamiento
- Género
- Descripción
- Link directo al juego
"""

import json
import re
from datetime import datetime
from typing import Optional, List, Dict, Any
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    pass


def parse_game_pass_coming_soon() -> List[Dict[str, Any]]:
    """
    Scrapia https://www.xbox.com/en-US/xbox-game-pass/games
    y retorna lista de próximos lanzamientos.
    
    Retorna:
        [
            {
                'id': 'starfield-expansion',
                'name': 'Starfield: Shattered Space',
                'release_date': '2026-06-15',
                'genre': 'RPG, Sci-Fi',
                'description': 'Expansión de Starfield...',
                'url': 'https://...',
                'coming_soon': True,
                'days_until': 45
            },
            ...
        ]
    """
    
    from playwright.sync_api import sync_playwright
    
    games = []
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )
        page = context.new_page()
        
        try:
            # Navegar a Game Pass
            page.goto("https://www.xbox.com/en-US/xbox-game-pass/games", 
                     wait_until="domcontentloaded", 
                     timeout=40_000)
            
            # Esperar a que carguen los juegos
            page.wait_for_selector("[data-game-id]", timeout=30_000)
            
            # Scroll para cargar más juegos (lazy loading)
            for _ in range(5):
                page.evaluate("window.scrollBy(0, window.innerHeight)")
                page.wait_for_timeout(1500)
            
            # Extraer juegos con fecha de lanzamiento
            game_elements = page.query_selector_all("[data-game-id]")
            
            for elem in game_elements:
                try:
                    # Estructura típica:
                    # <div data-game-id="...">
                    #   <h3>Nombre del juego</h3>
                    #   <span class="release-date">Coming Jun 15</span>
                    #   <span class="genre">RPG, Adventure</span>
                    #   <a href="/en-US/games/...">
                    
                    name_elem = elem.query_selector("h3, [data-title]")
                    name = name_elem.inner_text() if name_elem else None
                    
                    if not name:
                        continue
                    
                    # Buscar fecha de lanzamiento
                    date_elem = elem.query_selector("[data-release-date], .release-date, [class*='date']")
                    date_str = date_elem.inner_text() if date_elem else None
                    
                    # Buscar género
                    genre_elem = elem.query_selector("[data-genre], .genre, [class*='genre']")
                    genre = genre_elem.inner_text() if genre_elem else "Unknown"
                    
                    # Link del juego
                    link_elem = elem.query_selector("a[href*='/en-US/games/']")
                    url = link_elem.get_attribute("href") if link_elem else None
                    if url and not url.startswith("http"):
                        url = "https://www.xbox.com" + url
                    
                    # Extraer fecha en formato YYYY-MM-DD
                    release_date = None
                    if date_str:
                        # Patrones: "Coming Jun 15", "Jun 15, 2026", "Coming Soon", etc.
                        match = re.search(r'(\w+)\s+(\d{1,2})(?:,?\s*(\d{4}))?', date_str)
                        if match:
                            month_name = match.group(1)
                            day = int(match.group(2))
                            year = int(match.group(3)) if match.group(3) else 2026
                            
                            months = {
                                'Jan': 1, 'Feb': 2, 'Mar': 3, 'Apr': 4, 'May': 5, 'Jun': 6,
                                'Jul': 7, 'Aug': 8, 'Sep': 9, 'Oct': 10, 'Nov': 11, 'Dec': 12,
                                'January': 1, 'February': 2, 'March': 3, 'April': 4,
                                'June': 6, 'July': 7, 'August': 8, 'September': 9,
                                'October': 10, 'November': 11, 'December': 12
                            }
                            
                            if month_name in months:
                                month = months[month_name]
                                release_date = f"{year:04d}-{month:02d}-{day:02d}"
                    
                    if not release_date or release_date == "????-??-??":
                        release_date = None
                    
                    # Calcular días hasta lanzamiento
                    days_until = None
                    if release_date:
                        try:
                            launch = datetime.fromisoformat(release_date)
                            days_until = (launch - datetime.now()).days
                        except:
                            pass
                    
                    # Solo agregar si tiene fecha de lanzamiento
                    if release_date:
                        game_id = re.sub(r'[^a-z0-9-]', '-', name.lower()).strip('-')
                        
                        games.append({
                            'id': f"gamepass_{game_id}",
                            'name': name,
                            'release_date': release_date,
                            'release_date_str': date_str,
                            'genre': genre,
                            'url': url or f"https://www.xbox.com/en-US/xbox-game-pass/games",
                            'coming_soon': True,
                            'days_until': days_until or 0
                        })
                
                except Exception as e:
                    print(f"Error extrayendo juego: {e}")
                    continue
            
            # Ordenar por fecha de lanzamiento
            games.sort(key=lambda x: x['release_date'])
            
        except Exception as e:
            print(f"Error scrapeando Game Pass: {e}")
        
        finally:
            browser.close()
    
    return games


def get_gamepass_alerts(games: List[Dict], config: Dict) -> List[Dict]:
    """
    Compara la lista de juegos con el estado anterior
    y genera alertas para nuevos lanzamientos.
    
    Args:
        games: Lista de juegos obtenidos
        config: Configuración del bot
    
    Returns:
        Lista de alertas para Discord/ntfy
    """
    
    data_file = Path(__file__).parent / "data" / "gamepass_cache.json"
    
    # Cargar cache anterior
    previous_games = {}
    if data_file.exists():
        with open(data_file, 'r', encoding='utf-8') as f:
            previous_games = {g['id']: g for g in json.load(f)}
    
    # Guardar estado actual
    data_file.parent.mkdir(parents=True, exist_ok=True)
    with open(data_file, 'w', encoding='utf-8') as f:
        json.dump(games, f, ensure_ascii=False, indent=2)
    
    # Detectar nuevos juegos
    alerts = []
    for game in games:
        if game['id'] not in previous_games:
            # Juego nuevo → alerta
            days = game.get('days_until', 0)
            
            # Formatear fecha para mostrar
            try:
                date_obj = datetime.fromisoformat(game['release_date'])
                date_str = date_obj.strftime('%d de %B de %Y')  # "15 de junio de 2026"
            except:
                date_str = game['release_date']
            
            alerts.append({
                'type': 'gamepass_new',
                'title': f"🎮 Nuevo en Game Pass",
                'message': f"{game['name']}",
                'details': {
                    'release_date': date_str,
                    'genre': game.get('genre', 'Unknown'),
                    'days_until': days,
                    'url': game.get('url', '')
                }
            })
    
    return alerts


# Testeo rápido
if __name__ == "__main__":
    print("Scrapeando Game Pass...")
    games = parse_game_pass_coming_soon()
    
    if games:
        print(f"\nEncontrados {len(games)} próximos lanzamientos:\n")
        for g in games[:10]:  # Primeros 10
            print(f"  {g['name']}")
            print(f"    Fecha: {g['release_date']} ({g['days_until']} días)")
            print(f"    Género: {g['genre']}")
            print(f"    URL: {g['url']}\n")
    else:
        print("No se encontraron juegos próximos.")
