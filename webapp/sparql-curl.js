// The /sparql page's ready curl line: the endpoint as a program would call
// it, with this host in it. A classic script right after the line it fills,
// as the inline script it replaces was (no inline script: docs/dashboard.md,
// "The Content-Security-Policy").
document.querySelector('[data-curl]').textContent =
  `curl -u <user> -H 'Accept: text/csv' ${location.origin}/sparql \\\n` +
  `  --data-urlencode 'query=SELECT ?class (COUNT(?s) AS ?n) WHERE { ?s a ?class } GROUP BY ?class'`;
