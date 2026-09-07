const frame = document.getElementById('document');
const select = document.getElementById('changes');
const previous = document.getElementById('previous');
const next = document.getElementById('next');
const position = document.getElementById('position');

function navigate(index) {
  if (!select.options.length) {
    position.textContent = 'No changed passages';
    previous.disabled = next.disabled = true;
    return;
  }
  select.selectedIndex = Math.max(0, Math.min(index, select.options.length - 1));
  const id = select.value;
  const anchor = frame.contentDocument?.getElementById(id);
  anchor?.parentElement.scrollIntoView({behavior: 'instant', block: 'start'});
  history.replaceState(null, '', `#${id}`);
  position.textContent = `${select.selectedIndex + 1} of ${select.options.length} passages`;
  previous.disabled = select.selectedIndex === 0;
  next.disabled = select.selectedIndex === select.options.length - 1;
}

frame.addEventListener('load', () => {
  const initial = Array.from(select.options).findIndex(o => `#${o.value}` === location.hash);
  navigate(Math.max(initial, 0));
});
select.addEventListener('change', () => navigate(select.selectedIndex));
previous.addEventListener('click', () => navigate(select.selectedIndex - 1));
next.addEventListener('click', () => navigate(select.selectedIndex + 1));
let resizeTimer;
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => navigate(select.selectedIndex), 150);
});
