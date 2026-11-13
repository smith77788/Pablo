let homeProjectsRequest = 0;

function projectOverviewButton(label, action) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'btn btn-s';
  button.textContent = label;
  button.onclick = action;
  return button;
}

function renderHomeProjects(section, data) {
  const heading = document.createElement('div');
  heading.className = 'sec-row';
  const title = document.createElement('h2');
  title.className = 'sec';
  title.textContent = 'Проекты';
  heading.append(title, projectOverviewButton('Все проекты · ' + data.total, () => openNetworkBuilder()));
  section.replaceChildren(heading);
  const list = document.createElement('div');
  list.className = 'lst';
  list.style.margin = '0 16px 12px';
  section.append(list);
  if (!data.projects.length) {
    const create = projectOverviewButton('Создать проект', () => {
      openNetworkBuilder();
      openNetBuilderCreateModal();
    });
    list.append(create);
    return;
  }
  for (const project of data.projects) {
    const row = document.createElement('div');
    row.style.padding = '10px 12px';
    row.style.borderBottom = '1px solid var(--sep)';
    const open = projectOverviewButton(project.name, () => openNetworkBuilder(project.id));
    open.style.maxWidth = '100%';
    open.style.overflowWrap = 'anywhere';
    const stats = document.createElement('div');
    stats.className = 'hint';
    const counts = ['Узлов: ' + project.node_count];
    if (project.unavailable) counts.push('Нет в каталоге: ' + project.unavailable);
    if (project.in_flight) counts.push('Открытых операций: ' + project.in_flight);
    if (project.failed) counts.push('Сбоев за ' + data.issue_days + ' дн.: ' + project.failed);
    stats.textContent = counts.join(' · ');
    const actions = document.createElement('div');
    actions.style.cssText = 'display:flex;flex-wrap:wrap;gap:6px;margin-top:6px';
    if (project.issue_id) {
      actions.append(projectOverviewButton('Открыть сбой', () => openOpDetail(project.issue_id)));
    }
    actions.append(projectOverviewButton('Журнал', () => openOps(project.id)));
    row.append(open, stats, actions);
    list.append(row);
  }
}

async function loadHomeProjects() {
  const section = document.getElementById('homeProjects');
  if (!section) return;
  const request = ++homeProjectsRequest;
  const status = document.createElement('p');
  status.className = 'hint';
  status.setAttribute('role', 'status');
  status.textContent = 'Загружаем проекты…';
  section.replaceChildren(status);
  try {
    const data = await api('/api/miniapp/networks/project_overview');
    if (request !== homeProjectsRequest) return;
    if (data.enabled) {
      section.hidden = false;
      renderHomeProjects(section, data);
    } else {
      section.hidden = true;
      section.replaceChildren();
    }
  } catch (_) {
    if (request !== homeProjectsRequest) return;
    status.textContent = 'Не удалось загрузить проекты';
    section.append(projectOverviewButton('Повторить', loadHomeProjects));
  }
}
