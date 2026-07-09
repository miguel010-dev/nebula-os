const input = document.getElementById('search-input');
const searchBtn = document.getElementById('search-btn');
const results = document.getElementById('results');
const iframe = document.getElementById('browser-frame');

let currentPage = 1;
const resultsPerPage = 10;

// Função para exibir resultados de uma página específica
function displayResults(query, page = 1) {
  results.innerHTML = '';

  // Filtra todos os resultados
  const filtered = bigData.filter(item =>
    item.title.toLowerCase().includes(query.toLowerCase()) ||
    item.desc.toLowerCase().includes(query.toLowerCase())
  );

  if (filtered.length === 0) {
    results.innerHTML = '<p style="color:#ccc; text-align:center;">Nenhum resultado encontrado</p>';
    return;
  }

  const totalPages = Math.ceil(filtered.length / resultsPerPage);
  const startIndex = (page - 1) * resultsPerPage;
  const endIndex = startIndex + resultsPerPage;
  const pageResults = filtered.slice(startIndex, endIndex);

  pageResults.forEach(item => {
    const div = document.createElement('div');
    div.className = 'result-item';
    
    // Criar link que abre no iframe
    const link = document.createElement('a');
    link.href = "#"; // impede abrir em nova aba
    link.textContent = item.title;
    link.title = item.desc;
    link.className = 'result-link';
    link.style.display = 'block';
    link.style.cursor = 'pointer';
    link.addEventListener('click', e => {
      e.preventDefault();
      iframe.src = item.link; // abre dentro do iframe
    });

    const desc = document.createElement('div');
    desc.className = 'result-description';
    desc.textContent = item.desc;

    div.appendChild(link);
    div.appendChild(desc);
    results.appendChild(div);
  });

  // Navegação da página
  if (totalPages > 1) {
    const nav = document.createElement('div');
    nav.style.textAlign = 'center';
    nav.style.marginTop = '20px';
    
    if (page > 1) {
      const prev = document.createElement('button');
      prev.textContent = '⬅ Anterior';
      prev.style.marginRight = '10px';
      prev.onclick = () => {
        currentPage--;
        displayResults(input.value, currentPage);
      };
      nav.appendChild(prev);
    }

    if (page < totalPages) {
      const next = document.createElement('button');
      next.textContent = 'Próximo ➡';
      next.onclick = () => {
        currentPage++;
        displayResults(input.value, currentPage);
      };
      nav.appendChild(next);
    }

    results.appendChild(nav);
  }
}

// Eventos
searchBtn.addEventListener('click', () => {
  currentPage = 1;
  displayResults(input.value, currentPage);
});

input.addEventListener('input', () => {
  currentPage = 1;
  displayResults(input.value, currentPage);
});

input.addEventListener('keydown', e => {
  if (e.key === 'Enter') {
    e.preventDefault();
    currentPage = 1;
    displayResults(input.value, currentPage);
  }
});

// Exibe resultados padrão ao carregar a página
displayResults('');
