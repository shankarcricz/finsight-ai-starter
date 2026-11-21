// Runs in <head> before first paint so a saved light/dark choice never flashes the other theme.
// No saved choice (or storage blocked) = follow the operating system.
(function () {
  try {
    var t = localStorage.getItem('finsight-theme');
    if (t === 'light' || t === 'dark') document.documentElement.dataset.theme = t;
  } catch (e) {}
})();
