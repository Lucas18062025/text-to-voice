/**
 * Cloudflare Worker: sirve la UI (./web) y proxya /api/* al backend Python.
 *
 * Con el proxy la UI queda same-origin: no hace falta CORS ni cambiar
 * ninguna URL en el frontend. El backend se configura con la variable
 * API_ORIGIN (wrangler.toml [vars] o `wrangler secret put`).
 */
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
        return await fetch(target.toString(), request);
      } catch (error) {
        return Response.json(
          {
            detail:
              "No pude contactar el backend. Si está en free tier puede " +
              "estar arrancando: reintentá en ~40 s.",
            error: String(error),
          },
          { status: 502 },
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
