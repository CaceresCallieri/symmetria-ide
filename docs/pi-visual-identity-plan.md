# Pi — identidad visual y el cableado que la alimenta

> **Estado (10 Ago 2026):** la especificación visual está **cerrada**; nada está
> implementado. Este documento es autosuficiente: contiene todo lo necesario para
> construir sin volver a derivar nada.

Plan de trabajo manual. Se evaluó usar un run de `pi-flow` (Full SDLC) para la
parte de datos y se **descartó** — el razonamiento está en §8.

---

## 1. Qué se está construyendo, y por qué vale la pena

Pi es el único de los tres harnesses sin identidad visual propia: en un slot de la
IDE hoy se dibuja con el sparkle de Claude. Claude tiene su naranja (`#d97757`) y
OpenCode su azul (`#6f9bd6`); las dos son marcas ajenas que heredamos.

El argumento de fondo no es estético. **Claude y OpenCode son cajas negras**: la
IDE infiere ocupado/libre a partir de eventos de herramienta y no hay más. Pi
corre un grafo de 18 nodos que él mismo publica. Así que la identidad de pi puede
ser *honesta* de una forma que las otras dos no pueden: no una animación decorativa
sobre un estado binario, sino la lectura directa de una máquina de estados real.

Esa asimetría es el argumento de diseño más fuerte que hay acá, y es la razón por
la que el cableado de datos (§4, §5) no es plomería previa — **es el trabajo**.

---

## 2. Referencias

| Qué | Dónde |
|---|---|
| Banco visual (especificación cerrada, ejecutable) | `docs/reference/pi-identity-bench.html` — abrir en un navegador |
| Publicado | https://claude.ai/code/artifact/1a8b372a-e8a8-4892-8bed-a4c56b305d9b |
| Inspiración original | https://reactbits.dev/backgrounds/line-waves |
| Contrato pi↔IDE (autoridad, congela nombres de campo) | `docs/pi-harness-contract.md` |
| Issues | OWN-39 (identidad), OWN-40 (publicar el estado), OWN-41 (`PiWaves` + contrato) |

El banco es la especificación normativa: **lo que se porte a QML tiene que verse
así**. Los algoritmos están además transcritos en §6 por si el archivo se pierde.

---

## 3. Las tres piezas y dónde vive cada una

| # | Trabajo | Repo | Bloqueado por |
|---|---|---|---|
| A | Publicar el nodo del grafo al socket | `pi-agent` | nada |
| B | Contrato §2 + consumidor | `symmetria-ide` | nada (A y B se acuerdan juntos) |
| C | Portar las 11 animaciones a QML | `symmetria-agents-ui` | A + B para el mapeo por nodo |

**Parte de C ya se puede hacer sin A ni B:** las dos animaciones de dictado se
manejan con `isSttTarget` / `sttIsTranscribing`, propiedades que `AgentChip` ya
recibe inyectadas. Ése es el primer entregable visible.

⚠ **Otro agente trabaja en `pi-agent/lib/flow/`** (run de pi-flow vivo el 10 Ago,
worktree `flow/2026-08-10T18-09-41-194-caba94fc`). Todo el trabajo en pi-agent va
en worktree + PR.

---

## 4. Fase A — el productor (`pi-agent`)

**El dato ya existe y no cruza el cable.** `lib/flow/status-channel.ts`:

```ts
export const FLOW_STATUS_GLOBAL_KEY = "__piFlowState";

export interface FlowStatusBadge {
  node: FlowNodeId;      // "implement" | "review" | "gate:plan-review" | ...
  status: FlowRunStatus; // running | interrupted | done | failed
  phase?: string;        // "2/5"
  flow?: string;         // "Full SDLC" | "Open"
}
```

Es un `globalThis` **in-process**: lo escribe la extensión `flow-graph` y lo lee la
extensión `status-line` para su propio badge. Nunca sale al socket.

Lo que sí sale hacia la IDE es el envelope de `lib/symmetria/reporter.ts`, que
lleva otra cosa: `hook_event_name`, `tool_name`, `tool_path`, `session_id`, `cwd`.
Desde afuera, un pi corriendo `implement` y uno corriendo `review` son
indistinguibles.

**Decisiones a tomar:**

- **Qué envelope.** El `hook` se emite en transiciones de *herramienta*, que no son
  las transiciones de *nodo* — un nodo puede durar 20 minutos sin tocar una
  herramienta. Probablemente corresponde un tipo propio en vez de campos colgados
  del hook. §4 del contrato ya reserva ese razonamiento para la paridad de
  status-line, que iría por el envelope `status_line` existente.
- **Qué campos.** `node` y `status` son el mínimo; `phase` y `flow` son casi gratis.
- **Quién reporta.** Regla §2.3: **sólo la sesión raíz**. Los subagentes de pi-flow
  corren in-process y heredan la lista de extensiones, así que un emisor ingenuo
  publicaría hasta 12 nodos simultáneos bajo un `SYMMETRIA_AGENT_ID`.
- **Degradación.** Un run en modo `Open` no tiene nodo. La ausencia es estado
  normal, no error.

Vecino: **OWN-27** quiere el mismo modelo de estado para los agentes delegados. Si
los dos consumidores salen del mismo publisher, mejor.

---

## 5. Fase B — el contrato y el consumidor (`symmetria-ide`)

**El contrato es la autoridad.** `docs/pi-harness-contract.md` se declara así y
congela cada nombre de campo: *"nothing in this module may be renamed without the
other side"*. La IDE lee cada campo con `event.get(..., <default>)`, así que **un
error de tipeo degrada una función en silencio en vez de fallar**.

**Invariantes del lado consumidor:**

1. **El nodo NO pasa por `agent_activity.py`.** Ese módulo reduce todo a cinco
   estados (`starting`/`thinking`/`working`/`needs_permission`/`clearing`) que
   cualquier harness produce. El nodo de flow es un **eje ortogonal**: qué está
   haciendo, contra si está ocupado. Meterlo por ese embudo pierde justamente lo
   que lo hace valioso.
2. **Señal de notify propia.** Un campo nuevo en el registro del slot con su propia
   señal. Precedente exacto: `agentWorktree` usa `agentWorktreeChanged` en vez de
   colgarse de `termAgentsChanged`, precisamente para no hacer churn de delegates.
3. **Atribución por `session_id` + `cwd`**, vía `agent_registry.resolve_slot_for_event`
   — autoritativo sobre el `agent_id` que viene del env. (El daemon de CC 2.1.x
   congela ese id; ver `.claude/memory/reference/agent-sdk/daemon_freezes_agent_env.md`.)
4. **Ausencia de nodo es normal.** No loguear como error.

**Tests que valen:** parseo del envelope, atribución de slot, ausencia de nodo, y
que la señal no se emita para slots no enfocados.

---

## 6. Fase C — el port a QML (`symmetria-agents-ui`)

### 6.1 El seam ya está tallado

La rama **`feat/pi-white-chip`** (worktree `~/projects/symmetria-agents-ui-wt/pi-white-chip`,
commit `a891ad2`, aún no en `master`) ya hizo lo más importante: **separó forma de
acento** en `AgentChip.qml`.

```qml
readonly property bool _isOpenCodeForm: root.agentType === "opencode"
readonly property bool _isPi: root.agentType === "pi"
property color piAccent: "#ffffff"
readonly property color _accentColor: root._isOpenCodeForm ? root.openCodeAccent
    : root._isPi ? root.piAccent
    : root.claudeAccent
```

Hoy pi es *Claude-shaped y blanco*. `PiWaves` entra como una **tercera forma**
junto a `_isOpenCodeForm`. El blanco elegido ahí coincide con el tinte mercurio de
la especificación — no hay conflicto de identidad que resolver.

Empezar desde esa rama, no desde `master`.

### 6.2 Restricciones del módulo

- **QML puro, sin build step.** `install.sh` es un `rsync` a
  `/usr/lib/qt6/qml/Symmetria/Agents/UI` y lo dice en su encabezado. **Mantenerlo así.**
- **No hace falta shader.** Se evaluó `ShaderEffect` (Qt 6 exige `.qsb`
  precompilado) y no es necesario: todo se resuelve con `Shape`/`ShapePath` o
  `Canvas`.
- **Toolkit-puro:** sólo `QtQuick` (+ `QtQuick.Effects` para el `Colouriser`
  interno). Nada de quickshell ni singletons de shell.
- **Data-injected:** todo entra por propiedades con default.
- Preview: `qml -I qml preview/Preview.qml`. **Lo lanza el usuario** — abre ventana.

### 6.3 La primitiva única

Las once animaciones son **una sola operación de dibujo**: *polilínea con metal a
lo largo del arco*. Barras, espirales, trenzas y elipses son todas
`strokeAlong(pts, phase, width, opts)` con distinta lista de puntos. El componente
final es un `switch` sobre una función pura — mismo patrón que
`OpenCodeSoundwave._barHeight`.

**Una cápsula es un trazo con punta redonda de su propio ancho.** No hace falta
`roundRect`: `lineCap: round` sobre un segmento de largo `h − w` da exactamente la
cápsula del sprite de Claude.

### 6.4 El eje de identidad, que ya estaba establecido

Los componentes existentes se diferencian por **cómo se mueven**, no por qué
dibujan — el header de `OpenCodeSoundwave` lo dice: reusa el *movimiento* del
soundwave de Claude y cambia el material.

| Harness | Knob | Mecanismo |
|---|---|---|
| Claude | orgánico | sprite sheets dibujados a mano |
| OpenCode | mecánico | `robotFps: 10` cuantiza la fase a pasos discretos |
| **Pi** | **líquido** | **sin cuantizar** — fase continua, líneas desfasadas |

Pi es el opuesto exacto de OpenCode dentro de la misma arquitectura.

---

## 7. La especificación normativa

### 7.1 Constantes globales

```
LINES = 5
tinte mercurio: deep #232a31 · base #8e9aa6 · spec #f4f9ff · matte #59626d
metal = "cromo" (dos bandas) · grosor w = 0.75 · velocidad t × 1.5
fondo de escena STAGE_RGB = [9,12,16]   (#090c10)
MIN_LINE_PX = 0.6
s = min(ancho, alto)   ·   todo escala con s
```

### 7.2 El metal

```
cyc(a,b)      = min(|a−b| mod 1, 1 − |a−b| mod 1)     ← distancia cíclica
band(u,c,w)   = exp(−(cyc(u,c)/w)²)

metalAt(u, phase, opts):
    body = closed ? 0.92 : 0.42 + 0.58·sin(π·u)^0.55      ← FIJO a lo largo del trazo
    col  = mix(deep, base, body)
    p    = frac(phase)
    lum  = band(u, p,     max(0.13,  minBand))
         + 0.5 · band(u, p+0.5, max(0.085, minBand·0.7))   ← segunda banda = cromo
    col  = mix(col, spec, min(1, lum))
    return opaque(col, alpha)                              ← ¡NO alfa! ver §7.5

minBand = smooth ? 2.5/(n−1) : 0        n = cantidad de puntos
opaque(col, a) = rgb(mix(STAGE_RGB, col, a))   ← se compone contra el fondo
```

Modificadores de alpha por segmento, en `u ∈ [0,1]` a lo largo del trazo:

```
fade  : α ·= smoothstep(0, 0.11, u) · smoothstep(0, 0.11, 1−u)
tail  : α ·= smoothstep(0, 0.5, u)                    ← la cola interna se disuelve
taper : grosor = max(0.6, lw · (0.26 + 0.74·smoothstep(0, 0.55, u)))
```

### 7.3 Los tres presupuestos de detalle

```
detailLine (s)         = max(8,            min(48, round(s/6)))
detailCurve(s, vueltas)= max(⌈26·vueltas⌉, min(64, round(s/5)))
detailWave (s, ciclos) = max(⌈12·ciclos⌉,  min(56, round(s/5)))
```

### 7.4 Las once animaciones

Todas: `phase` es el argumento de `metalAt`; `rot −45°` significa rotar el lienzo.

| # | Nombre | Nodo | Algoritmo |
|---|---|---|---|
| 1 | **Soundwave** | dictado · escuchando | 5 barras verticales. `span=0.78s`, `barW=(span/5)·0.52·w`, `minH=0.14s`, `maxH=0.86s`. Pesos `[0.32,0.62,1.0,0.62,0.32]`. `pulse = 0.5+0.5·sin(2πt − dist·1.1)` con `dist=|i−2|`. `h = minH+(maxH−minH)·peso·pulse`. Cápsula = trazo de ancho `barW`. `phase = 0.8t + 0.07i` |
| 2 | **Barrido** | dictado · transcribiendo | Igual, otra función de altura: `sweep = min(frac(0.9t)·5, 5−ε)`, `d = distancia envolvente(i, sweep)`, `h = minH+(maxH−minH)·exp(−d²/0.8)` |
| 3 | **Deriva** | `discourse` · `explore` | 5 rectas, `rot −45°`. `base=(k−2)·0.17s`, `n=x/s`, `y = base + s·(0.11·sin(2.1n + 1.5t + 0.5k) + 0.06·sin(3.4n − 0.9t + 0.9k))`. `lw=0.05s·w`, `segs=detailWave(diag, 1.1)`, `phase = 0.9t + 0.15k` |
| 4 | **Caracol** | `plan` | Espiral redonda, rotación **constante** (nada nace ni muere → sin ciclo perceptible). `R=0.44s`, `rIn=0.22R`, `ARMS=5`, `vueltas=0.78`, `spin=0.42t`. `base = spin + k·2π/5`, `r = rIn+(R−rIn)·u^0.7`, `θ = base + u·vueltas·2π`, `y ·= 0.94`. Onda de presencia: `swell = 0.5+0.5·cos(2π(0.26t − k/5))`, `dim = 0.22+0.78·swell`. `tail+taper+smooth`, `lw=0.045s·w`, `phase = 0.5t + 0.06k` |
| 5 | **Vórtice** | `plan` (alternativa) | Brazos que nacen, crecen y mueren. `R=0.46s`, `ARMS=8`, `vueltas=0.68`. `life = frac(0.19t + k/8)`, `α = smoothstep(0,0.26,life)·(1−smoothstep(0.7,1,life))`, `scale = 0.16+0.92·life`, **`spin = 0.6t + k·2π/8 + 0.7·life`** (giro desacoplado — ver §7.5), `r = R·scale·(0.26+0.74u)`. `tail+taper+smooth`, `lw=0.042s·w`, `phase = 0.55t + 0.04k` |
| 6 | **Trenza** | `implement` | 5 hebras con profundidad `DEPTH=[1.0, 0.18, 0.72, 0.1, 0.34]`, dibujadas **de la más tenue a la más brillante** (las cercanas ocluyen). `off=k·2π/5`, `rise=(k−2)·0.055s`, `amp=0.2s`, `y = cy + rise + amp·sin(3πu − 2t + off) + 0.3·amp·sin(5πu + 1.3t − off)`. `lw = 0.05s·w·(0.58+0.55d)`, `dim = 0.16+0.84d`, `fade`, `segs = detailWave(max(ancho,s), 2.5)`, `phase = 1.1t + 0.18k` |
| 7 | **Órbitas** | `review` · `triage` | 9 elipses que **se trazan**, no se desvanecen. `life = frac(0.17t + k/9)`, `head = smoothstep(0,1,clamp(life/0.6))`, `tail = smoothstep(0,1,clamp((life−0.4)/0.6))`, `arc = head−tail` (saltear si ≤0.004). Se dibuja el sub-arco `u ∈ [tail, head]`. `ang = 0.05t + k·2.399963` (ángulo áureo), `rx = 0.4s·(0.62+0.36·frac(0.37k))`, `ry = rx·(0.17+0.2·(0.5+0.5·sin(1.7k+0.4t)))`. `lw=0.034s·w`, `smooth`, `closed` si `arc>0.95`, `phase = 0.6t + 0.11k` |
| 8 | **Alineación** | `verify` · `checks` | **Dos tandas a medio ciclo.** Por tanda: `p = frac(0.22t + offset)`, saltear si `p>0.74`. `enter=ss(p/0.2)`, `align=ss((p−0.2)/0.24)`, `exit=ss((p−0.52)/0.2)`. `travel = (restX − (span+ancho)·(1−enter)) + exit·(ancho+span)`. `off = (1−align)·0.22s·sin(2.3k+1.1)`, `tilt = (1−align)·0.26·sin(1.7k)`, `y = y0 + k·0.15s`. `span=0.84s`, `dim = 0.48+0.52·align`, `fade`, `lw=0.05s·w`, `phase = 0.7t + 0.1k` |
| 9 | **Convergencia** | `commit` · `integrate` | **Dos tandas a medio ciclo.** `p = frac(0.2t + offset)`, saltear si `p>0.73`. `enter=ss(p/0.13)`, `gather=ss((p−0.15)/0.27)`, `exit=ss((p−0.6)/0.12)`. `x = (restX − (span+ancho)·(1−enter)) + exit·(ancho+span)`, `spread = (k−2)·0.15s·(1−gather)`. `span=0.8s`, `dim = 1−0.7·exit`, `fade`, `lw=0.05s·w`, `phase = 0.9t + 0.12k` |
| 10 | **Índice** | `gate:plan-review` | **ESTÁTICO.** 5 renglones horizontales alineados a la izquierda, largos `[1.0, 0.68, 0.88, 0.55, 0.78]·span`, `span=0.82s`, `gapY=0.16s`, `lw=0.055s·w`. `phase = 0.28 + 0.06k` — **fijo, no depende de `t`**: ni el brillo se mueve |
| 11 | **Reposo** | `gate:mid` · `gate:end` | **ESTÁTICO.** 5 rectas paralelas, `rot −45°`, largo `0.62·diag`, `gap=0.17s`, `lw=0.05s·w·0.85`, **mate**, `phase = 0` |

### 7.5 Lecciones de render — no reaprenderlas

Todas se pagaron con horas. Están ordenadas por cuánto cuesta redescubrirlas.

**1 · El conteo de puntos sale de lo que la FORMA contiene, no de su tamaño.**
El mismo error apareció tres veces con tres síntomas distintos:

- una espiral se veía **hexagonal** a 16 px (6 segmentos para 1.15 vueltas);
- la trenza se veía como un **zigzag anguloso**: lleva 2.5 ciclos y se muestreaba
  6 veces — **2.4 muestras por ciclo contra un límite de Nyquist de 2**. No era
  una curva mal dibujada, era una curva imposible de reconstruir;
- la cola del vórtice **se cortaba en pedazos**: el afinado la llevaba a 0.131 px
  (un cuarto de píxel real a dpr 2) y Canvas eso lo dibuja como nada.

De ahí los tres presupuestos de §7.3 y el piso `MIN_LINE_PX`.

**2 · La transparencia produce cuentas, y sólo se nota en lo tenue.**
Cada segmento es un trazo propio con punta redonda, así que en cada unión se
superponen dos puntas; compuesto con alfa `a`, ese solape queda en `1−(1−a)²`.
Invisible con `a=1`, un **collar de perlas** con `a=0.16`. La solución es no usar
alfa: mezclar el color contra el fondo y dibujar **opaco** (`opaque()` en §7.2).
De yapa las hebras se **ocluyen** en vez de mezclarse, que es como se comportan
las cintas reales.

**3 · El cuerpo del metal es FIJO; sólo viaja el brillo.**
Una revisión hizo que el sombreado de cuerpo se moviera con la fase y la línea
entera latía. Se nota primero en la trenza. No reintroducirlo.

**4 · Desacoplar ejes que parecen uno solo.**
En el vórtice el giro estaba atado al ciclo de vida (`spin = t·0.85 + life·vueltas·2π`).
Como `life` también manejaba la escala, **cada brazo más grande quedaba rotado
proporcionalmente más**, y los brazos anidados se encadenaban ópticamente en *una
sola espiral*: se dibujaban cinco y se veía uno. El análisis cuadro a cuadro lo
reportó como «un brazo que crece 4 s y se retrae de golpe».
En la trenza, las cinco hebras compartían presencia y ninguna se podía seguir.
**Regla:** cada eje que modula la animación —giro, escala, presencia, fase del
brillo— es su propia propiedad con su propio reloj.

**5 · Envolventes con ceros reales.**
`sin(π·life)^0.55` vale **0.218** en `life=0.02`: un brazo no entraba, aparecía.
Exponente > 1 (o `smoothstep`) da 0.012 en el mismo punto.

**6 · Al portar a QML, tres de estos desaparecen solos.**
Un `ShapePath` con `LinearGradient` a lo largo del path **es continuo**, así que
el coloreado por segmento, las cuentas y la banda mínima dejan de existir. Lo que
**sí hay que conservar es el afinado de grosor** (`taper`): no es un artefacto, es
diseño — es lo que hace que una cola termine en punta y no en tope.

---

## 8. La gramática semántica

Estas dos reglas gobiernan cualquier animación futura, no sólo las once.

**Quieto quiere decir que te toca a vos.** Todo lo que fluye es la máquina
trabajando; todo lo que se detiene espera una decisión humana. No hace falta un
badge — el material lo dice.

Los dos estáticos se distinguen por **cómo** están quietos:

- **Índice** — *resuelto*: renglones ordenados de largos desiguales. «Esto terminó
  y está listo para que lo leas.»
- **Reposo** — *detenido*: diagonales rectas, paralelas, en mate. «Esto se frenó.»

**Corolario, y es regla del sistema:** ninguna animación con principio y final
puede dejar el cuadro vacío. Vacío es el grado extremo de quieto, así que un hueco
de 300 ms en `commit` no es una imperfección estética — es una frase que dice lo
contrario de lo que está pasando. De ahí que Convergencia y Alineación corran **dos
tandas solapadas** en vez de una sola en bucle.

**Dirección.** Alineación y Convergencia viajan izquierda→derecha. En Alineación
lo torcido entra por un lado y lo derecho sale por el otro, así que el eje
*significa* algo: es la dirección en que el trabajo se vuelve correcto. Vale
tratarlo como parte de la gramática.

---

## 9. Por qué NO se usa un run de pi-flow

Se evaluó a fondo el 10 Ago 2026. Tres razones, en orden de peso:

1. **Las animaciones no tienen criterio de aceptación testeable.** La columna
   vertebral de pi-flow es `test-authoring → implement → checks → verify`, o sea
   rojo→verde. «El caracol se ve fluido» no lo asserta ningún test; `test-authoring`
   escribiría tests estructurales vacíos que pasan sin decir nada.
2. **Un run activo por repo.** `.pi/flow/current` es un puntero único por proyecto.
   `pi-agent` está tomado por el run del otro agente, así que la Fase A no podría
   arrancar igual.
3. **`discover-checks` no encuentra las suites.** El run vivo del 10 Ago está
   `degraded: true` con `checksAbsent: true` sobre **pi-agent mismo**, que tiene
   `vitest.config.ts` y `tests/`. Nuestro run degradaría igual, y el argumento
   entero para usar el flow era «esta mitad sí es verificable».

**Si se retoma la idea**, el paso cero es que `discover-checks` vea las suites:
`PYTHONPATH=src python -m pytest tests/` en la IDE, `vitest` en pi-agent. Eso
merece arreglarse por sí solo, independientemente de este plan.

---

## 10. Decisiones abiertas

- **Caracol contra Vórtice** para `plan`. El que no gane no se descarta: su destino
  natural es `decompose`, el pariente más cercano.
- **Cinco nodos sin asignar**: `decompose`, `test-authoring`, `discover-checks`,
  `regression`, `phase-advance`/`explain`. Candidatos a **reusar** antes que a
  inventar — `test-authoring` es pariente de verify, `regression` es review con el
  resultado al revés.
- **El acento de pi.** `feat/pi-white-chip` fijó `#ffffff`; la especificación usa
  mercurio (base `#8e9aa6`, spec `#f4f9ff`). Compatibles, pero hay que elegir el
  valor único que va al módulo. Va junto a `claudeAccent`/`openCodeAccent`, que
  están marcados *"identity colors, intentionally not themed"* — **no va a `Theme.qml`**.

---

## 11. Orden sugerido de construcción

1. **`PiSoundwave` en `symmetria-agents-ui`**, desde `feat/pi-white-chip`. No
   depende de A ni de B: `isSttTarget` y `sttIsTranscribing` ya existen. Entrega
   valor visible y valida la primitiva `strokeAlong` en QML de una vez.
2. **Fase A + B en paralelo**, acordando §2 del contrato primero (la IDE es la
   autoridad; pi implementa contra especificación congelada). Worktree + PR en los
   dos repos.
3. **El resto de las formas**, de a una, contra el preview.
4. **El mapeo nodo → animación**, que es lo único que necesita A y B terminados.
