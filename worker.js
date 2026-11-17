/**
 * Cloudflare Worker: sirve la UI (./web) y proxya /api/* al backend Python.
 *
 * Con el proxy la UI queda same-origin: no hace falta CORS ni cambiar
 * ninguna URL en el frontend. El backend se configura con la variable
 * API_ORIGIN (wrangler.toml [vars] o `wrangler secret put`).
 */

// Mensaje unico para cualquier caida del backend, para que el visitante
// no vea "Error 530" ni detalles de infraestructura.
const OFFLINE =
  "El servicio de voz no está disponible en este momento.";
export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname.startsWith("/api/")) {
      const origin = env.API_ORIGIN;
      if (!origin) {
        return Response.json(
          { detail: "API_ORIGIN no está configurado en el Worker." },
          { status: 500 },
        );
      }

      const target = new URL(url.pathname + url.search, origin);
      try {
        // 90 s: cubre el cold start de un free tier (~40-50 s).
        // Sin timeout, un origen inexistente cuelga hasta que Cloudflare
        // mata la invocación y devuelve un 502 genérico sin explicación.
        const upstream = await fetch(target.toString(), {
          method: request.method,
          headers: request.headers,
          body:
            request.method === "GET" || request.method === "HEAD"
              ? undefined
              : request.body,
          signal: AbortSignal.timeout(90000),
        });

        // Un túnel caído no "falla" desde el punto de vista del Worker:
        // Cloudflare responde 530 con una página HTML (error 1016). Si la
        // dejamos pasar, la UI muestra "Error 530". La traducimos a JSON.
        if (upstream.status >= 500) {
          const type = upstream.headers.get("content-type") || "";
          if (!type.includes("application/json")) {
            return Response.json(
              { detail: OFFLINE, upstream_status: upstream.status },
              { status: 503 },
            );
          }
        }

        return upstream;
      } catch (error) {
        const timedOut =
          error?.name === "TimeoutError" || error?.name === "AbortError";
        return Response.json(
          {
            detail: timedOut
              ? `${OFFLINE} Si el backend estaba arrancando, esperá ~40 s y reintentá.`
              : OFFLINE,
            code: timedOut ? "upstream_timeout" : "upstream_unreachable",
          },
          { status: 503 },
        );
      }
    }

    return env.ASSETS.fetch(request);
  },

  /**
   * Cron cada 10 min: mantiene despierto el backend de free tier
   * (Render duerme a los 15 min de inactividad).
   */
  async scheduled(_controller, env, ctx) {
    if (!env.API_ORIGIN) return;
    ctx.waitUntil(
      fetch(new URL("/api/health", env.API_ORIGIN), {
        signal: AbortSignal.timeout(25000),
      }).catch(() => {}),
    );
  },
};
