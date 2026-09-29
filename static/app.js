// Live repayment calculator on the loan application form
(function () {
  var f = document.getElementById('loan-form');
  if (!f) return;
  var $ = function (id) { return document.getElementById(id); };
  var R = function (v) { return 'R ' + v.toLocaleString('en-ZA', {minimumFractionDigits: 2, maximumFractionDigits: 2}); };
  function calc() {
    var p = parseFloat($('principal').value) || 0, r = parseFloat($('monthly_rate').value) || 0, n = parseInt($('term_months').value) || 1;
    var interest = p * r / 100 * n, total = p + interest;
    $('c-interest').textContent = R(interest); $('c-total').textContent = R(total);
    $('c-each').textContent = R(total / n) + ' x ' + n;
  }
  f.addEventListener('input', calc); calc();
})();
