// Vercel Edge Middleware: HTTP Basic Auth gate for the whole site.
// Set DASHBOARD_PASSWORD (and optionally DASHBOARD_USER, default "pau") as
// environment variables in the Vercel project settings.

export const config = {
  matcher: "/(.*)",
};

export default function middleware(request) {
  const password = process.env.DASHBOARD_PASSWORD || "";
  const username = process.env.DASHBOARD_USER || "pau";

  // If no password configured, fail closed.
  if (!password) {
    return new Response("Dashboard password not configured", {
      status: 503,
      headers: { "Content-Type": "text/plain; charset=utf-8" },
    });
  }

  const auth = request.headers.get("authorization") || "";
  const expected = "Basic " + btoa(`${username}:${password}`);

  if (auth !== expected) {
    return new Response("Authentication required", {
      status: 401,
      headers: {
        "WWW-Authenticate": 'Basic realm="Sports Dashboard"',
        "Content-Type": "text/plain; charset=utf-8",
      },
    });
  }
  // Authenticated — fall through to static asset serving.
}
