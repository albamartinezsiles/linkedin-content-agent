"""
LinkedIn Content Agent
======================
Genera un post de LinkedIn investigado + carrusel visual en PNG,
y crea un issue en GitHub con todo para revisión.

Variables de entorno requeridas:
    GEMINI_API_KEY   - API key de Google AI Studio (gratis)
    GITHUB_TOKEN     - provisto automáticamente por GitHub Actions
    GITHUB_REPOSITORY - provisto automáticamente por GitHub Actions
    PROFILE_YAML     - contenido del config/profile.yaml (secret)
"""

import os
import sys
import json
import random
import yaml
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

import google.generativeai as genai
from playwright.sync_api import sync_playwright
import requests


# ----------------------------------------------------------------------------
# Rutas y constantes
# ----------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
TEMPLATES_DIR = ROOT / "templates"
BUILD_DIR = ROOT / "build"
STATE_PATH = CONFIG_DIR / "state.json"

MADRID_TZ = timezone(timedelta(hours=1))


# ----------------------------------------------------------------------------
# Carga de configuración
# ----------------------------------------------------------------------------
def load_config() -> dict:
    with open(CONFIG_DIR / "topics.yaml", "r", encoding="utf-8") as f:
        topics = yaml.safe_load(f)

    # profile.yaml viene del secret PROFILE_YAML en Actions
    profile_path = CONFIG_DIR / "profile.yaml"
    if "PROFILE_YAML" in os.environ:
        profile_path.write_text(os.environ["PROFILE_YAML"], encoding="utf-8")

    if not profile_path.exists():
        raise FileNotFoundError(
            "Falta config/profile.yaml. Copia profile.example.yaml y rellénalo, "
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
    """Rotación circular con peso — evita repetir pilar y salta ocasionalmente
    pilares con weight < 1."""
    pillars = topics["pillars"]
    next_idx = (state["last_pillar_index"] + 1) % len(pillars)
    tries = 0
    while random.random() > pillars[next_idx].get("weight", 1.0) and tries < len(pillars):
        next_idx = (next_idx + 1) % len(pillars)
        tries += 1
    state["last_pillar_index"] = next_idx
    return pillars[next_idx]


# ----------------------------------------------------------------------------
# Llamada a Gemini
# ----------------------------------------------------------------------------
SYSTEM_INSTRUCTION = """Eres un estratega de contenido de LinkedIn para perfiles técnicos en español.

Tu trabajo es generar UN post de alta calidad que:

1. Investiga tendencias recientes (últimas 2 semanas) relacionadas con el pilar asignado usando Google Search.
2. Elige un ángulo NICHO — no el tema obvio, sino el contraintuitivo o poco tratado.
3. Escribe el post respetando exactamente la voz definida.
4. Propone 5 slides para un carrusel visual (portada, 3 de contenido, cierre con CTA).
5. Sugiere cómo este post posiciona al perfil respecto a oportunidades laborales.

FORMATO DE RESPUESTA — devuelve SOLO un objeto JSON válido, sin markdown, sin ```json, sin comentarios:

{
  "research_summary": "2-3 frases sobre qué has encontrado investigando y por qué elegiste este ángulo",
  "angle_chosen": "frase corta describiendo el ángulo único",
  "hook_primary": "primera línea del post que para el scroll",
  "hook_alternative": "gancho alternativo",
  "post_body": "post completo listo para copiar, con \\n para saltos de línea, incluyendo gancho al inicio y hashtags al final",
  "slides": [
    {"type": "cover", "eyebrow": "PILAR EN MAYÚSCULAS", "title": "título grande de portada", "highlight_word": "palabra del título a destacar en rosa", "subtitle": "subtítulo de 1 línea"},
    {"type": "content", "label": "01", "heading": "título del slide", "body": "texto del cuerpo, máx 25 palabras"},
    {"type": "highlight", "big_number": "dato o cifra corta", "caption": "qué significa ese dato"},
    {"type": "content", "label": "02", "heading": "título del slide", "body": "texto del cuerpo, máx 25 palabras"},
    {"type": "outro", "title": "pregunta o afirmación de cierre", "highlight_word": "palabra a destacar", "cta_text": "invitación al engagement"}
  ],
  "positioning_note": "1-2 frases sobre qué tipo de oferta/contacto puede atraer",
  "best_publish_time": "HH:MM en horario España, con razonamiento breve"
}

Reglas estrictas para los slides:
- 'highlight_word' debe ser UNA palabra o expresión corta que aparezca literalmente en 'title'.
- 'big_number' debe ser corto (máx 6 caracteres): "23%", "3h", "400+", "0€", etc.
- Los textos de slide deben ser LEGIBLES a golpe de vista — no párrafos.
- Los slides deben poder leerse sin el post: cuentan la misma historia resumida.
"""


def build_user_prompt(profile: dict, voice: dict, pillar: dict, recent_angles: list) -> str:
    avoid_list = "\n".join(f"  - {x}" for x in voice["avoid"])
    do_list = "\n".join(f"  - {x}" for x in voice["do"])
    recent = "; ".join(recent_angles[-6:]) if recent_angles else "ninguno"
    hooks = "\n".join(f"  - {h}" for h in pillar.get("example_hooks", []))

    return f"""PERFIL:
Nombre: {profile['name']}
Rol: {profile['role']}
Ubicación: {profile['location']}
Años de experiencia: {profile['years_experience']}
Enfoque: {profile['focus']}
Diferenciadores: {', '.join(profile['differentiators'])}
Oportunidades que busca: {profile.get('target_opportunities', 'desarrollo profesional general')}

VOZ:
Estilo: {voice['style']}
Longitud objetivo: {voice['length_chars'][0]}-{voice['length_chars'][1]} caracteres
Hashtags: {voice['hashtags_count'][0]}-{voice['hashtags_count'][1]} al final, minúsculas

EVITAR:
{avoid_list}

HACER:
{do_list}

PILAR DE HOY: {pillar['name']} (id: {pillar['id']})
Ángulo del pilar: {pillar['angle']}
Ejemplos de ganchos que encajan (úsalos como referencia de tono, no copies):
{hooks}

ÁNGULOS RECIENTES YA USADOS (no repetir): {recent}

TAREA:
1. Investiga con Google Search qué está pasando ESTA SEMANA sobre este pilar.
2. Elige un ángulo nicho y fundamentado.
3. Genera el JSON completo. SOLO el JSON, nada más.
"""


def call_gemini(config: dict, pillar: dict, recent_angles: list) -> dict:
    api_key = os.environ["GEMINI_API_KEY"]
    genai.configure(api_key=api_key)

    gen_cfg = config["topics"]["generation"]
    user_prompt = build_user_prompt(
        config["profile"], config["topics"]["voice"], pillar, recent_angles
    )

    # Google Search grounding para Gemini 2.5
    tools = [{"google_search": {}}] if gen_cfg.get("enable_search") else None

    model = genai.GenerativeModel(
        model_name=gen_cfg["model"],
        system_instruction=SYSTEM_INSTRUCTION,
        tools=tools,
        generation_config={
            "temperature": gen_cfg["temperature"],
            "max_output_tokens": 4096,
        },
    )

    response = model.generate_content(user_prompt)
    text = response.text.strip()

    # Limpiar fences por si el modelo los añade a pesar de la instrucción
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)

    # Intento parseo directo; si falla, busco el primer bloque JSON válido
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise


# ----------------------------------------------------------------------------
# Renderizado de slides HTML → PNG
# ----------------------------------------------------------------------------
def escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def highlight_in_title(title: str, word: str) -> str:
    """Envuelve la palabra a destacar en un <span class='highlight'>."""
    if not word or word not in title:
        return escape_html(title)
    safe_title = escape_html(title)
    safe_word = escape_html(word)
    return safe_title.replace(
        safe_word, f'<span class="highlight">{safe_word}</span>', 1
    )


def render_slide_content(slide: dict) -> tuple[str, str]:
    """Devuelve (clase CSS, HTML interno) para un slide."""
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
        <div class="cta-box">Sígueme para más <span class="arrow">→</span></div>
        """
        return "outro", inner

    # default: content
    inner = f"""
    <div class="label">{escape_html(slide.get('label', ''))}</div>
    <h2>{escape_html(slide.get('heading', ''))}</h2>
    <div class="body-text">{escape_html(slide.get('body', ''))}</div>
    """
    return "content", inner


def render_slides_to_png(slides: list, pillar_name: str) -> list[Path]:
    """Renderiza cada slide como PNG usando Playwright."""
    BUILD_DIR.mkdir(exist_ok=True)

    # Leer plantilla base y CSS
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

    # Capturar con Playwright
    png_paths = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(
            viewport={"width": 1080, "height": 1350},
            device_scale_factor=2,  # retina = crisp
        )
        page = context.new_page()

        for i, html_path in enumerate(html_files, start=1):
            page.goto(f"file://{html_path}")
            # Esperar a que las fuentes de Google se carguen
            page.evaluate("document.fonts.ready")
            page.wait_for_timeout(800)

            png_path = BUILD_DIR / f"slide_{i:02d}.png"
            page.screenshot(path=str(png_path), full_page=False, omit_background=False)
            png_paths.append(png_path)

        browser.close()

    return png_paths


# ----------------------------------------------------------------------------
# Creación del issue en GitHub
# ----------------------------------------------------------------------------
def create_github_issue(
    result: dict, pillar: dict, png_paths: list[Path], run_id: str | None
) -> None:
    token = os.environ["GITHUB_TOKEN"]
    repo = os.environ["GITHUB_REPOSITORY"]
    now = datetime.now(MADRID_TZ)

    visual_links = ""
    if run_id:
        artifact_url = f"https://github.com/{repo}/actions/runs/{run_id}"
        visual_links = (
            f"\n\n📎 **Descarga el carrusel** (PNGs 1080×1350): "
            f"[artifact del workflow]({artifact_url})\n"
        )

    slides_preview = "\n".join(
        f"- **Slide {i+1}** ({s.get('type')}): "
        f"{s.get('title') or s.get('heading') or s.get('big_number', '')}"
        for i, s in enumerate(result.get("slides", []))
    )

    body = f"""## 📌 Post de {pillar['name']}

> {result.get('angle_chosen', '')}

**Hora sugerida:** {result.get('best_publish_time', '—')}

---

### 🔍 Investigación
{result.get('research_summary', '')}

---

### ✍️ Post listo para copiar

```
{result.get('post_body', '')}
```

---

### 🪝 Gancho alternativo

> {result.get('hook_alternative', '')}

---

### 🖼️ Carrusel ({len(png_paths)} slides)

{slides_preview}
{visual_links}

---

### 🎯 Por qué este post posiciona

{result.get('positioning_note', '')}

---

<sub>Generado por linkedin-content-agent · {now:%Y-%m-%d %H:%M} Madrid · Cierra este issue cuando publiques.</sub>
"""

    title = f"📝 {pillar['name']} — {now:%d/%m} — {result.get('angle_chosen', '')[:50]}"

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


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main() -> int:
    config = load_config()
    state = load_state()
    pillar = pick_pillar(config["topics"], state)

    print(f"[{datetime.now(MADRID_TZ):%Y-%m-%d %H:%M}] Pilar: {pillar['name']}")

    print("Llamando a Gemini con Google Search...")
    result = call_gemini(config, pillar, state.get("recent_angles", []))
    print(f"Ángulo elegido: {result.get('angle_chosen')}")

    print("Renderizando slides...")
    png_paths = render_slides_to_png(result["slides"], pillar["name"])
    print(f"Generados {len(png_paths)} PNGs en build/")

    print("Creando issue en GitHub...")
    run_id = os.environ.get("GITHUB_RUN_ID")
    create_github_issue(result, pillar, png_paths, run_id)

    # Actualizar estado
    state.setdefault("recent_angles", []).append(result.get("angle_chosen", "")[:80])
    state["recent_angles"] = state["recent_angles"][-12:]
    save_state(state)

    print("Listo.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
