/* Search the generated documentation topics without fetching files or loading a service. */
(() => {
  const input = document.querySelector(".doc-search input");
  const list = document.getElementById("doc-search-results");
  const status = document.getElementById("doc-search-status");
  if (!input || !list || !status) return;

  const topics = [...list.children].map((node) => ({
    node,
    title: node.dataset.searchTitle.toLowerCase(),
    page: node.dataset.searchPage.toLowerCase(),
    text: node.dataset.searchText,
    haystack:
      `${node.dataset.searchTitle} ${node.dataset.searchPage} ${node.dataset.searchText}`.toLowerCase(),
  }));

  function search() {
    const query = input.value.trim().toLowerCase();
    const terms = query.split(/\s+/).filter(Boolean);
    const matches = terms.length
      ? topics
          .filter((topic) => terms.every((term) => topic.haystack.includes(term)))
          .map((topic) => ({
            ...topic,
            score:
              (topic.title.includes(query) ? 100 : 0) +
              terms.reduce(
                (score, term) =>
                  score +
                  (topic.title.includes(term) ? 10 : 0) +
                  (topic.page.includes(term) ? 3 : 0),
                0,
              ),
          }))
          .sort((a, b) => b.score - a.score)
      : [];

    for (const topic of topics) topic.node.hidden = true;
    for (const topic of matches.slice(0, 20)) {
      const start = Math.max(0, topic.text.toLowerCase().indexOf(terms[0]) - 60);
      const end = start + 180;
      topic.node.querySelector(".search-result-excerpt").textContent =
        `${start ? "… " : ""}${topic.text.slice(start, end)}${end < topic.text.length ? " …" : ""}`;
      topic.node.hidden = false;
      list.append(topic.node);
    }
    status.textContent = !terms.length
      ? "Enter a topic, feature or error message to search."
      : !matches.length
        ? "No results. Try fewer words or another topic."
        : matches.length > 20
          ? `Showing 20 of ${matches.length} results. Add words to narrow your search.`
          : `${matches.length} ${matches.length === 1 ? "result" : "results"}.`;
  }

  input.value = (new URLSearchParams(location.search).get("q") ?? "").slice(0, 160);
  input.addEventListener("input", search);
  search();
})();
