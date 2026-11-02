// Where the page reads its two data files (live.json and snapshot.json).
// Empty: next to the page, in data/ (local preview, GitHub Pages).
// For a host that is not redeployed on every update (Vercel), point it at the repository the Lab publishes the data to (a separate one, so the host never tries to build it):
window.RELIOPS_DATA_BASE = "https://raw.githubusercontent.com/OmarElzero/ReliOps-Lab-data/results-data/data/";
