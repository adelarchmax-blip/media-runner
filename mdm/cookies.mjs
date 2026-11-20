// The sign-in browser's cookies (login/session.py): read through Chrome's DevTools on 127.0.0.1 only,
// printed as one JSON line for the script that sends them to the controller. Never written to the log.
import puppeteer from "puppeteer-core";

const browser = await puppeteer.connect({ browserURL: "http://127.0.0.1:9222", defaultViewport: null });
try {
  const cdp = await browser.target().createCDPSession();
  const { cookies } = await cdp.send("Storage.getCookies");
  const ua = await browser.userAgent();
  process.stdout.write(JSON.stringify({
    cookies: cookies.map((c) => ({ name: c.name, value: c.value, domain: c.domain, path: c.path, expires: c.expires,
      secure: c.secure, httpOnly: c.httpOnly, sameSite: c.sameSite })),
    ua,
  }) + "\n");
} finally {
  browser.disconnect();
}
