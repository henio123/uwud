document.addEventListener('DOMContentLoaded', () => {
  const toggle = document.getElementById('darkModeToggle');
  const body = document.body;

  // Załaduj ustawienie z localStorage
  if (localStorage.getItem('darkMode') === 'enabled') {
    body.classList.add('dark-mode');
    toggle.textContent = 'Tryb jasny';
  }

  toggle.addEventListener('click', () => {
    body.classList.toggle('dark-mode');
    if(body.classList.contains('dark-mode')) {
      toggle.textContent = 'Tryb jasny';
      localStorage.setItem('darkMode', 'enabled');
    } else {
      toggle.textContent = 'Tryb ciemny';
      localStorage.setItem('darkMode', 'disabled');
    }
  });
});
