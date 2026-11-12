// The script of the /gateways page the gateway renders itself
// (scripts/web-gateway.py, _render_gateways_html), loaded only while a
// gateway shows a pairing code to scan.
//
// A code is hidden until an image actually arrives: while the gateway is
// still generating one, its /qr answers JSON, and a visible <img> would be a
// broken-image icon. A fresh code is fetched every 20 s (a pairing code
// expires), and the page reloads after a minute to pick up the link state.
//
// It used to be written into the page, the show and hide as handler
// attributes on the image. It is a file of the dashboard's own because the
// pages' Content-Security-Policy runs scripts only from such files and runs
// no handler attributes (docs/dashboard.md, "The Content-Security-Policy").
// A classic script at the end of the body, where the inline one stood.
(function () {
  var codes = document.querySelectorAll('img.qr');
  Array.prototype.forEach.call(codes, function (img) {
    img.addEventListener('load', function () { img.style.display = ''; });
    img.addEventListener('error', function () { img.style.display = 'none'; });
    // The first image may have arrived before this script ran.
    if (img.complete && img.naturalWidth > 0) img.style.display = '';
  });
  setInterval(function () {
    Array.prototype.forEach.call(codes, function (img) {
      var u = new URL(img.getAttribute('src'), location);
      u.searchParams.set('ts', Date.now());
      img.src = u.toString();
    });
  }, 20000);
  setTimeout(function () { location.reload(); }, 60000);
})();
