/* table.js — dependency-free client-side sort + filter for flax-control.
 *
 *  (a) Any <table class="sortable"> becomes click-to-sort by <th>.
 *      Numeric columns sort numerically; everything else as text.
 *      Clicking toggles asc/desc. Add data-nosort to a <th> to skip it.
 *  (b) <input class="table-filter" data-table="ID"> hides rows in the
 *      target table whose text doesn't contain the (case-insensitive) query.
 */
(function () {
  "use strict";

  function cellText(row, idx) {
    var cell = row.children[idx];
    return cell ? cell.textContent.trim() : "";
  }

  function asNumber(s) {
    // Strip commas; treat empty / dash as NaN.
    if (s === "" || s === "—" || s === "-") return NaN;
    s = s.replace(/,/g, "");
    // Only treat as numeric when the WHOLE cell is a number. parseFloat alone
    // parses a leading number out of any string -- "2026-06-25T..." -> 2026,
    // "172.17.0.122" -> 172 -- so dates/IPs all collapse to one value and the
    // numeric branch leaves them unsorted. Anything not a pure number falls
    // through to the text comparator (localeCompare numeric:true), which sorts
    // ISO timestamps chronologically and IPs/versions naturally.
    if (!/^-?\d+(\.\d+)?$/.test(s)) return NaN;
    return parseFloat(s);
  }

  function makeSortable(table) {
    var thead = table.tHead;
    if (!thead) return;
    var headRow = thead.rows[thead.rows.length - 1];
    if (!headRow) return;
    var ths = Array.prototype.slice.call(headRow.cells);

    ths.forEach(function (th, idx) {
      if (th.hasAttribute("data-nosort")) return;
      // Append a sort arrow indicator.
      var arrow = document.createElement("span");
      arrow.className = "arrow";
      arrow.textContent = "↕";
      th.appendChild(arrow);

      th.addEventListener("click", function () {
        var asc = !(th.classList.contains("sorted-asc"));
        // Reset siblings.
        ths.forEach(function (other) {
          other.classList.remove("sorted-asc", "sorted-desc");
          var a = other.querySelector(".arrow");
          if (a) a.textContent = "↕";
        });
        th.classList.add(asc ? "sorted-asc" : "sorted-desc");
        var a = th.querySelector(".arrow");
        if (a) a.textContent = asc ? "↑" : "↓";

        var tbody = table.tBodies[0];
        if (!tbody) return;
        var rows = Array.prototype.slice.call(tbody.rows);
        rows.sort(function (r1, r2) {
          var t1 = cellText(r1, idx), t2 = cellText(r2, idx);
          var n1 = asNumber(t1), n2 = asNumber(t2);
          var cmp;
          if (!isNaN(n1) && !isNaN(n2)) {
            cmp = n1 - n2;
          } else {
            cmp = t1.localeCompare(t2, undefined, { numeric: true });
          }
          return asc ? cmp : -cmp;
        });
        rows.forEach(function (r) { tbody.appendChild(r); });
      });
    });
  }

  function wireFilter(input) {
    var targetId = input.getAttribute("data-table");
    var table = document.getElementById(targetId);
    if (!table) return;
    input.addEventListener("input", function () {
      var q = input.value.toLowerCase();
      var tbody = table.tBodies[0];
      if (!tbody) return;
      Array.prototype.slice.call(tbody.rows).forEach(function (row) {
        var hit = q === "" || row.textContent.toLowerCase().indexOf(q) !== -1;
        row.style.display = hit ? "" : "none";
      });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    Array.prototype.slice.call(document.querySelectorAll("table.sortable"))
      .forEach(makeSortable);
    Array.prototype.slice.call(document.querySelectorAll("input.table-filter"))
      .forEach(wireFilter);
  });
})();
