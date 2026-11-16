const byId = (id) => document.getElementById(id);

function formatDate(value) {
  return new Date(`${value}T12:00:00`).toLocaleDateString(undefined, {
    weekday: 'long', month: 'long', day: 'numeric'
  });
}

function createTokenList(tokens, emptyLabel) {
  if (!tokens.length) {
    const empty = document.createElement('span');
    empty.className = 'waiting-room-muted';
    empty.textContent = emptyLabel;
    return empty;
  }
  const list = document.createElement('div');
  list.className = 'waiting-token-list';
  tokens.forEach((token) => {
    const item = document.createElement('span');
    item.className = 'waiting-token';
    item.textContent = `#${token}`;
    list.append(item);
  });
  return list;
}

function renderWaitingRoom(data) {
  byId('waitingRoomDate').textContent = formatDate(data.date);
  const container = byId('waitingRoomDoctors');
  container.replaceChildren();
  data.doctors.forEach((doctor) => {
    const card = document.createElement('article');
    card.className = 'waiting-doctor-card';
    const heading = document.createElement('div');
    heading.className = 'waiting-doctor-heading';
    const title = document.createElement('h2');
    title.textContent = doctor.doctorName;
    const specialty = document.createElement('span');
    specialty.textContent = doctor.specialty;
    heading.append(title, specialty);

    const current = document.createElement('section');
    current.className = 'waiting-current';
    const currentLabel = document.createElement('p');
    currentLabel.textContent = 'Now in consultation';
    const currentToken = document.createElement('strong');
    currentToken.textContent = doctor.currentToken === null ? '—' : `#${doctor.currentToken}`;
    current.append(currentLabel, currentToken);

    const waiting = document.createElement('section');
    waiting.className = 'waiting-next';
    const waitingLabel = document.createElement('p');
    waitingLabel.textContent = 'Waiting tokens';
    waiting.append(waitingLabel, createTokenList(doctor.waitingTokens, 'No one is waiting'));
    card.append(heading, current, waiting);
    container.append(card);
  });
  byId('waitingRoomEmpty').hidden = data.doctors.length !== 0;
  container.hidden = data.doctors.length === 0;
  byId('waitingRoomUpdated').textContent =
    `Last updated ${new Date().toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })} · refreshes every 10 seconds`;
}

async function refreshWaitingRoom() {
  const response = await fetch('/api/waiting-room', { cache: 'no-store' });
  let data;
  try {
    data = await response.json();
  } catch {
    throw new Error('The server returned an invalid response.');
  }
  if (!response.ok) throw new Error(data.error || 'The waiting room could not be refreshed.');
  if (!Array.isArray(data.doctors)) throw new Error('The server returned an invalid waiting-room schedule.');
  renderWaitingRoom(data);
  byId('waitingRoomError').hidden = true;
}

function showWaitingRoomError(error) {
  byId('waitingRoomError').textContent = error.message;
  byId('waitingRoomError').hidden = false;
}

refreshWaitingRoom().catch(showWaitingRoomError);
setInterval(() => refreshWaitingRoom().catch(showWaitingRoomError), 10000);
