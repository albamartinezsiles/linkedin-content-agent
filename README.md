# linkedin-content-agent

> Un agente que investiga, escribe y maqueta mis posts de LinkedIn 3 veces por semana. Corre gratis en GitHub Actions, me notifica por GitHub Issues, y cuesta 0€/mes.

No publica automáticamente en LinkedIn. Esto es deliberado — el valor está en quitarme el bloqueo creativo, no en publicar al vacío sin revisar.

---

## Qué hace

Cada **lunes, miércoles y viernes a las 08:30 (hora Madrid)**, un workflow de GitHub Actions:

1. Rota al siguiente **pilar de contenido** (dev general, WordPress avanzado, IA para devs, UX/UI, carrera).
2. Llama a **Gemini 2.5 Flash con Google Search** para investigar qué está pasando esa semana en ese nicho.
3. Le pide al modelo que elija un **ángulo nicho** (no el tema obvio) y genere:
   - Post listo para copiar-pegar
   - Dos versiones del gancho (primaria y alternativa)
   - Guión de **5 slides** para el carrusel visual
   - Nota de posicionamiento profesional
   - Hora óptima de publicación
4. Renderiza los 5 slides como HTML con mi estética personal y los captura a PNG con **Playwright**.
5. Crea un **issue en este repo** con el post completo y enlaza los PNGs del carrusel como artifact descargable.
6. GitHub me manda el email automáticamente porque soy el owner del repo.

Solo tengo que abrir el issue, revisar el texto, descargar el carrusel, subirlo a LinkedIn y cerrar el issue. 5 minutos.

## Por qué así y no de otra forma

**¿Por qué no publicar automáticamente?** LinkedIn penaliza el contenido que huele a bot. Revisar y editar 2 frases antes de publicar es justo lo que hace que el post funcione.

**¿Por qué GitHub Issues en lugar de email?** Porque me queda gratis, sin configurar SMTP ni API keys de email, con búsqueda y etiquetas incluidas. Cada post es un issue que puedo cerrar cuando lo publico: mini-CRM de contenido gratis.

**¿Por qué Gemini y no Claude/GPT?** Porque tiene tier gratis real (1500 requests/día) y Google Search integrado sin API adicional. Para 12 posts al mes sobra x100.

**¿Por qué HTML + Playwright en vez de SVG o una API de imagen generativa?**
- Imagen generativa: mala calidad para texto legible, estética inconsistente
- SVG puro: limitado para maquetar tipografía de verdad
- HTML + Playwright: control total, fuentes de Google, flexbox/grid, y la captura es pixel-perfect

## Stack

| Pieza | Tecnología | Coste |
|---|---|---|
| Modelo + búsqueda | Gemini 2.5 Flash + Google Search | 0€ (tier gratis) |
| Orquestación | GitHub Actions (cron) | 0€ (repos públicos) |
| Maquetación visual | HTML + CSS + Playwright | 0€ |
| Notificación | GitHub Issues + email nativo | 0€ |
| Storage de posts | Git (state.json + issues) | 0€ |

**Coste real total: 0€/mes.**

## Arquitectura

```
linkedin-content-agent/
├── .github/workflows/generate.yml   # cron L/X/V + trigger manual
├── config/
│   ├── topics.yaml                  # pilares, voz, reglas (público)
│   ├── profile.example.yaml         # plantilla de perfil
│   ├── profile.yaml                 # perfil real (IGNORADO, va en secret)
│   └── state.json                   # estado de rotación (auto-gestionado)
├── src/
│   └── generate.py                  # todo el pipeline
├── templates/
│   ├── base.html                    # plantilla única
│   └── styles.css                   # design tokens + tipos de slide
├── requirements.txt
└── README.md
```

## Instalación

### 1. Fork o clona este repo

Tiene que ser **público** para usar GitHub Actions ilimitado. Si lo haces privado, tienes 2000 min/mes que igualmente sobran para este caso.

### 2. Configura los secrets

Settings → Secrets and variables → Actions → New repository secret:

| Secret | Cómo conseguirlo |
|---|---|
| `GEMINI_API_KEY` | [aistudio.google.com](https://aistudio.google.com/apikey) → Create API key. Gratis, sin tarjeta. |
| `PROFILE_YAML` | Copia `config/profile.example.yaml`, rellena con tus datos, pega el **contenido completo** del archivo aquí como valor del secret. |

Nada más. `GITHUB_TOKEN` lo da el workflow solo.

### 3. Ajusta tus pilares

Edita `config/topics.yaml`. Cada pilar tiene un `angle` que es el prompt implícito para ese tipo de post. Cuanto más específico, mejor. Si quieres quitar un pilar o añadir uno nuevo, sigue el formato existente.

### 4. Ajusta la estética del carrusel

Edita `templates/styles.css`. Las variables CSS al inicio del archivo (`--brand-accent`, `--font-display`, etc.) son tus design tokens. Cámbialos y tus slides cambian de golpe. La tipografía viene de Google Fonts, así que puedes poner cualquier familia de ahí.

### 5. Primer disparo manual

Actions → Generate LinkedIn Post → Run workflow. En 1-2 minutos tienes tu primer issue.

## Cómo extenderlo

- **Añadir un pilar**: bloque nuevo en `pillars:` de `topics.yaml` con `id`, `name`, `weight`, `angle` y 3 ejemplos de ganchos.
- **Cambiar frecuencia**: edita el cron en `.github/workflows/generate.yml`.
- **Ajustar la voz**: edita `voice.avoid` y `voice.do` con frases que odies o te encanten. El modelo las respeta literalmente.
- **Nuevos tipos de slide**: añade una clase en `styles.css` y un branch en `render_slide_content()` en `generate.py`.

## Los primeros posts van a sonar un poco a IA — es normal

El prompt está muy afinado, pero las primeras 2-3 semanas vas a identificar frases que te suenan a chatbot. La solución: apúntalas en `voice.avoid` del YAML. Después de 4-5 iteraciones el sistema aprende tu voz real via prompt engineering. Es parte del proceso, no lo abandones el primer día.

## Licencia

MIT. Cógelo, modifícalo, haz tu versión. Si lo usas, me gustaría saberlo — escríbeme por LinkedIn.
