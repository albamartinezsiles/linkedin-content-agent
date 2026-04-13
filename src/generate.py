"""
LinkedIn Content Agent v2
==========================
Genera un post de LinkedIn anclado a una noticia reciente + carrusel visual,
y crea un issue en GitHub con todo para revisión.

Cambios v2:
- Flujo en dos pasos: investigar -> elegir noticia -> escribir post
- Nivel técnico calibrado "dev con 3 años"
- Fallback a opinion piece técnico si no hay noticias buenas
- Aprovecha search_queries y good_sources del topics.yaml
"""

import os
import sys
import json
import random
import time
import yaml
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

from google import genai
from google.genai import types as genai_types
from playwright.sync_api import sync_playwright
import requests


ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
TEMPLATES_DIR = ROOT / "templates"
BUILD_DIR = ROOT / "build"
STATE_PATH = CONFIG_DIR / "state.json"

MADRID_TZ = timezone(timedelta(hours=1))


def load_config() -> dict:
    with open(CONFIG_DIR / "topics.yaml", "r", encoding="utf-8") as f:
        topics = yaml.safe_load(f)

    profile_path = CONFIG_DIR / "profile.yaml"
    if "PROFILE_YAML" in os.environ:
        profile_path.write_text(os.environ["PROFILE_YAML"], encoding="utf-8")

    if not profile_path.exists():
        raise FileNotFoundError(
            "Falta config/profile.yaml. Copia profile.example.yaml y rellenalo, "
            "o define la variable de entorno PROFILE_YAML."
        )

    with open(profile_path, "r", encoding="utf-8") as f:
        profile = yaml.safe_load(f)

    return {"topics": topics, "profile": profile}


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"last_pillar_index": -1, "recent_angles": []}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(
        json.dumps(state, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def pick_pillar(topics: dict, state: dict) -> dict:
    pillars = topics["pillars"]
    next_idx = (state["last_pillar_index"] + 1) % len(pillars)
    tries = 0
    while random.random() > pillars[next_idx].get("weight", 1.0) and tries < len(pillars):
        next_idx = (next_idx + 1) % len(pillars)
        tries += 1
    state["last_pillar_index"] = next_idx
    return pillars[next_idx]


SYSTEM_INSTRUCTION = """Eres un estratega de contenido para perfiles tecnicos en LinkedIn.

Tu trabajo NO es generar contenido generico de autoayuda tech. Tu trabajo es producir
posts ANCLADOS EN HECHOS REALES Y RECIENTES, con opinion fundamentada y nivel tecnico
calibrado al perfil del autor.

FLUJO OBLIGATORIO EN 2 FASES:

FASE 1 - INVESTIGACION:
Usa Google Search para buscar noticias y releases reales de los ultimos 14 dias
sobre el pilar asignado. Usa las search_queries que te paso como punto de partida
y prioriza los dominios listados en good_sources.

Criterios de una BUENA noticia:
- Release oficial de un framework, libreria o herramienta con nombre concreto
- Cambio de API o deprecation con impacto real en devs
- Benchmark, estudio o informe con numeros verificables
- Bug conocido documentado con fix
- Debate publico reciente entre figuras del sector con posturas claras

Criterios de una MALA noticia (descartar):
- Articulos tipo "10 tips para X" o "como ser mejor dev"
- Opiniones genericas sin datos
- Contenido mas viejo de 14 dias
- Tutoriales sin noticia detras
- Hype sin sustancia

FASE 2 - DECISION Y ESCRITURA:

Si encontraste al menos UNA noticia buena:
- Elige la mas jugosa
- Escribe el post ANCLADO a esa noticia, citandola explicitamente en el cuerpo
- Incluye una opinion personal concreta, no neutra
- Aporta contexto tecnico que el lector promedio no tendria
- Si es posible, incluye un dato, numero o mini ejemplo de codigo

Si NO encontraste noticias buenas:
- Cambia a modo "opinion_piece"
- Elige un problema REAL y concreto del pilar (no abstracto)
- Escribe desde la experiencia tecnica del autor con ejemplos especificos
- DEBE incluir nombres propios de herramientas, versiones o un mini ejemplo
- NO debe sonar a reflexion generica

NIVEL TECNICO CALIBRADO:
- Publico objetivo: desarrollador/a con 3 anios de experiencia
- SI ASUME que conoce: HTML, CSS, JS moderno, React basico, Git, npm, APIs REST
- NO ASUME que conoce: Server Components, Suspense, Edge runtime, WASM,
  AST, compilers internals, algoritmos de caching avanzados
- Cuando uses un termino nicho, explicalo en 1 frase
- Usa nombres propios sin miedo (Next.js, Vite, Astro, Tailwind, Bun)
- Evita corporate, buzzwords, "en el mundo actual"

FORMATO DE RESPUESTA:
Devuelve SOLO un objeto JSON valido, sin bloques markdown.

Estructura:
{
  "research_phase": {
    "queries_used": ["q1", "q2", "q3"],
    "news_found": [
      {"title": "t", "url": "u", "date": "YYYY-MM-DD", "why_relevant": "1 frase"}
    ],
    "mode": "news_anchored" o "opinion_piece",
    "mode_reason": "por que este modo"
  },
  "angle_chosen": "frase corta del angulo",
  "hook_primary": "primera linea del post",
  "hook_alternative": "gancho alternativo",
  "post_body": "post completo listo para copiar con \\n reales. Si news_anchored debe citar la noticia. Debe tener ejemplos concretos. Hashtags al final.",
  "slides": [
    {"type": "cover", "eyebrow": "PILAR", "title": "titulo", "highlight_word": "palabra", "subtitle": "subtitulo"},
    {"type": "content", "label": "01", "heading": "titulo", "body": "max 25 palabras"},
    {"type": "highlight", "big_number": "max 6 chars", "caption": "significado"},
    {"type": "content", "label": "02", "heading": "titulo", "body": "max 25 palabras"},
    {"type": "outro", "title": "cierre", "highlight_word": "palabra", "cta_text": "invitacion"}
  ],
  "positioning_note": "1-2 frases sobre oportunidades",
  "best_publish_time": "HH:MM con razonamiento"
}

Reglas:
- highlight_word debe aparecer LITERALMENTE en title
- big_number max 6 chars
- post_body: 1000-1500 caracteres
- Los slides cuentan la historia resumida del post
"""


def build_user_prompt(profile: dict, voice: dict, pillar: dict, recent_angles: list) -> str:
    avoid_list = "\n".join(f"  - {x}" for x in voice["avoid"])
    do_list = "\n".join(f"  - {x}" for x in voice["do"])
    recent = "; ".join(recent_angles[-6:]) if recent_angles else "ninguno"
    hooks = "\n".join(f"  - {h}" for h in pillar.get("example_hooks", []))
    queries = "\n".join(f"  - {q}" for q in pillar.get("search_queries", []))
    sources = "\n".join(f"  - {s}" for s in pillar.get("good_sources", []))
    technical_level = voice.get("technical_level", "dev con 3 anios")
    today = datetime.now(MADRID_TZ).strftime("%Y-%m-%d")

    return f"""FECHA DE HOY: {today}

PERFIL DEL AUTOR:
Nombre: {profile['name']}
Rol: {profile['role']}
Ubicacion: {profile['location']}
Anios de experiencia: {profile['years_experience']}
Enfoque: {profile['focus']}
Diferenciadores: {', '.join(profile['differentiators'])}
Oportunidades que busca: {profile.get('target_opportunities', 'desarrollo profesional general')}

VOZ:
Estilo: {voice['style']}
Longitud: {voice['length_chars'][0]}-{voice['length_chars'][1]} caracteres
Hashtags: {voice['hashtags_count'][0]}-{voice['hashtags_count'][1]}, minusculas
Nivel tecnico objetivo: {technical_level}

EVITAR:
{avoid_list}

HACER:
{do_list}

========================================
PILAR DE HOY: {pillar['name']} (id: {pillar['id']})
========================================

Angulo del pilar:
{pillar['angle']}

QUERIES DE BUSQUEDA (empieza por estas, anade si hace falta):
{queries}

FUENTES A PRIORIZAR si aparecen:
{sources}

Ejemplos de ganchos del estilo buscado (referencia, NO copiar):
{hooks}

ANGULOS RECIENTES YA USADOS (no repetir): {recent}

========================================
TAREA
========================================

Ejecuta el flujo de 2 fases:
1. Investiga con Google Search usando las queries sugeridas
2. Evalua resultados segun criterios buenos/malos
3. Decide modo (news_anchored o opinion_piece)
4. Genera el JSON completo

Devuelve SOLO el JSON.
"""


def call_gemini(config: dict, pillar: dict, recent_angles: list) -> dict:
    api_key = os.environ["GEMINI_API_KEY"]
    client = genai.Client(api_key=api_key)

    gen_cfg = config["topics"]["generation"]
    user_prompt = build_user_prompt(
        config["profile"], config["topics"]["voice"], pillar, recent_angles
    )

    tools = None
    if gen_cfg.get("enable_search"):
        tools = [genai_types.Tool(google_search=genai_types.GoogleSearch())]

    models_to_try = [gen_cfg["model"]] + gen_cfg.get("fallback_models", [])
    last_error = None

    for model_name in models_to_try:
        print(f"Intentando con modelo: {model_name}", flush=True)
        max_retries = 3
        succeeded = False
        for attempt in range(max_retries):
            try:
                resp = client.models.generate_content(
                    model=model_name,
                    contents=user_prompt,
                    config=genai_types.GenerateContentConfig(
                        system_instruction=SYSTEM_INSTRUCTION,
                        temperature=gen_cfg["temperature"],
                        max_output_tokens=6144,
                        tools=tools,
                    ),
                )
                if resp is None or resp.text is None:
                    raise RuntimeError(f"{model_name} devolvió respuesta vacía")
                print(f"Respuesta obtenida con modelo: {model_name}", flush=True)
                text = resp.text.strip()
                succeeded = True
                break
            except Exception as e:
                last_error = e
                err_str = str(e)
                is_transient = any(k in err_str for k in ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "respuesta vacía")) or "overloaded" in err_str.lower()
                if not is_transient:
                    raise
                if attempt < max_retries - 1:
                    wait = 2 ** attempt * 5
                    print(f"{model_name} error transitorio - reintentando en {wait}s (intento {attempt+1}/{max_retries})...", flush=True)
                    time.sleep(wait)
                else:
                    print(f"{model_name} agotó reintentos, probando siguiente modelo...", flush=True)
        if succeeded:
            break
    else:
        raise RuntimeError(f"Todos los modelos fallaron. Último error: {last_error}")

    # text ya asignado en el bucle

    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise


def escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def highlight_in_title(title: str, word: str) -> str:
    if not word or word not in title:
        return escape_html(title)
    safe_title = escape_html(title)
    safe_word = escape_html(word)
    return safe_title.replace(
        safe_word, f'<span class="highlight">{safe_word}</span>', 1
    )


def render_slide_content(slide: dict):
    stype = slide.get("type", "content")

    if stype == "cover":
        inner = f"""
        <div class="eyebrow">{escape_html(slide.get('eyebrow', ''))}</div>
        <h1>{highlight_in_title(slide.get('title', ''), slide.get('highlight_word', ''))}</h1>
        <div class="subtitle">{escape_html(slide.get('subtitle', ''))}</div>
        """
        return "cover", inner

    if stype == "highlight":
        inner = f"""
        <div class="big-number">{escape_html(slide.get('big_number', ''))}</div>
        <div class="caption">{escape_html(slide.get('caption', ''))}</div>
        """
        return "highlight", inner

    if stype == "outro":
        inner = f"""
        <h2>{highlight_in_title(slide.get('title', ''), slide.get('highlight_word', ''))}</h2>
        <div class="cta-text">{escape_html(slide.get('cta_text', ''))}</div>
        <div class="cta-box">Sigueme para mas <span class="arrow">-></span></div>
        """
        return "outro", inner

    inner = f"""
    <div class="label">{escape_html(slide.get('label', ''))}</div>
    <h2>{escape_html(slide.get('heading', ''))}</h2>
    <div class="body-text">{escape_html(slide.get('body', ''))}</div>
    """
    return "content", inner


def render_slides_to_png(slides: list, pillar_name: str) -> list:
    BUILD_DIR.mkdir(exist_ok=True)

    base_html = (TEMPLATES_DIR / "base.html").read_text(encoding="utf-8")
    css = (TEMPLATES_DIR / "styles.css").read_text(encoding="utf-8")

    total = len(slides)
    html_files = []

    for i, slide in enumerate(slides, start=1):
        css_class, inner = render_slide_content(slide)
        html = (
            base_html.replace("{{TITLE}}", f"Slide {i}")
            .replace("{{SLIDE_CLASS}}", css_class)
            .replace("{{SLIDE_CONTENT}}", inner)
            .replace("{{SLIDE_INDEX}}", f"{i:02d}")
            .replace("{{SLIDE_TOTAL}}", f"{total:02d}")
            .replace(
                '<link rel="stylesheet" href="styles.css">',
                f"<style>{css}</style>",
            )
        )
        html_path = BUILD_DIR / f"slide_{i:02d}.html"
        html_path.write_text(html, encoding="utf-8")
        html_files.append(html_path)

    png_paths = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(
            viewport={"width": 1080, "height": 1350},
            device_scale_factor=2,
        )
        page = context.new_page()

        for i, html_path in enumerate(html_files, start=1):
            page.goto(f"file://{html_path}")
            page.evaluate("document.fonts.ready")
            page.wait_for_timeout(800)

            png_path = BUILD_DIR / f"slide_{i:02d}.png"
            page.screenshot(path=str(png_path), full_page=False, omit_background=False)
            png_paths.append(png_path)

        browser.close()

    return png_paths


def format_research_phase(research: dict) -> str:
    if not research:
        return "(sin datos de investigacion)"

    lines = []
    lines.append(f"**Modo:** `{research.get('mode', 'desconocido')}`")
    lines.append(f"**Razon:** {research.get('mode_reason', '-')}")

    queries = research.get("queries_used", [])
    if queries:
        lines.append("\n**Queries usadas:**")
        for q in queries:
            lines.append(f"- `{q}`")

    news = research.get("news_found", [])
    if news:
        lines.append("\n**Noticias encontradas:**")
        for n in news:
            title = n.get("title", "")
            url = n.get("url", "")
            date = n.get("date", "")
            why = n.get("why_relevant", "")
            if url:
                lines.append(f"- [{title}]({url}) - {date} - {why}")
            else:
                lines.append(f"- {title} - {date} - {why}")

    return "\n".join(lines)


def create_github_issue(result, pillar, png_paths, run_id):
    token = os.environ["GITHUB_TOKEN"]
    repo = os.environ["GITHUB_REPOSITORY"]
    now = datetime.now(MADRID_TZ)

    visual_links = ""
    if run_id:
        artifact_url = f"https://github.com/{repo}/actions/runs/{run_id}"
        visual_links = (
            f"\n\nDescarga el carrusel (PNGs 1080x1350): "
            f"[artifact del workflow]({artifact_url})\n"
        )

    slides_preview = "\n".join(
        f"- **Slide {i+1}** ({s.get('type')}): "
        f"{s.get('title') or s.get('heading') or s.get('big_number', '')}"
        for i, s in enumerate(result.get("slides", []))
    )

    research_md = format_research_phase(result.get("research_phase", {}))

    body = f"""## Post de {pillar['name']}

> {result.get('angle_chosen', '')}

**Hora sugerida:** {result.get('best_publish_time', '-')}

---

### Fase de investigacion
{research_md}

---

### Post listo para copiar

{result.get('post_body', '')}

---

### Gancho alternativo

> {result.get('hook_alternative', '')}

---

### Carrusel ({len(png_paths)} slides)

{slides_preview}
{visual_links}

---

### Por que este post posiciona

{result.get('positioning_note', '')}

---

<sub>Generado por linkedin-content-agent v2 - {now:%Y-%m-%d %H:%M} Madrid - Cierra este issue cuando publiques.</sub>
"""

    title = f"Post {pillar['name']} - {now:%d/%m} - {result.get('angle_chosen', '')[:50]}"

    url = f"https://api.github.com/repos/{repo}/issues"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload = {
        "title": title,
        "body": body,
        "labels": [pillar["id"], "linkedin-post", "pending"],
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    print(f"Issue creado: {resp.json()['html_url']}")


def main() -> int:
    config = load_config()
    state = load_state()
    pillar = pick_pillar(config["topics"], state)

    print(f"[{datetime.now(MADRID_TZ):%Y-%m-%d %H:%M}] Pilar: {pillar['name']}")

    print("Llamando a Gemini con Google Search (flujo 2 fases)...")
    result = call_gemini(config, pillar, state.get("recent_angles", []))

    research = result.get("research_phase", {})
    print(f"Modo: {research.get('mode', 'desconocido')}")
    print(f"Angulo elegido: {result.get('angle_chosen')}")

    print("Renderizando slides...")
    png_paths = render_slides_to_png(result["slides"], pillar["name"])
    print(f"Generados {len(png_paths)} PNGs en build/")

    print("Creando issue en GitHub...")
    run_id = os.environ.get("GITHUB_RUN_ID")
    create_github_issue(result, pillar, png_paths, run_id)

    state.setdefault("recent_angles", []).append(result.get("angle_chosen", "")[:80])
    state["recent_angles"] = state["recent_angles"][-12:]
    save_state(state)

    print("Listo.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
