const DAY_NAMES = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const AVATAR_COLORS = ['green', 'violet', 'rose', 'blue', 'gold'];
const TOKEN_API_VERSION_ERROR =
  'This page is connected to an older clinic server. Stop it and start the Python app with python app.py.';

let doctors = [];
let selectedDoctorId = null;
let nextTokenEstimate = null;
let patientDoctorsSignature = '';
let displayedAvailabilitySignature = '';
let activeTrackingToken = null;

const byId = (id) => document.getElementById(id);

function localDate(date = new Date()) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

function formatTime(time) {
  const [hours, minutes] = time.split(':').map(Number);
  return `${String(hours % 12 || 12).padStart(2, '0')}:${String(minutes).padStart(2, '0')} ${hours < 12 ? 'AM' : 'PM'}`;
}

function formatDate(date) {
  return new Date(`${date}T12:00:00`).toLocaleDateString(undefined, {
    weekday: 'long', month: 'long', day: 'numeric'
  });
}

function initials(name) {
  return name.trim().split(/\s+/).slice(0, 2).map((part) => part[0].toUpperCase()).join('');
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (character) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[character]);
}

function bookingTrackingUrl(token) {
  return `${window.location.origin}/?booking=${encodeURIComponent(token)}`;
}

function renderBookingNotifications(notifications) {
  const container = byId('bookingNotifications');
  container.innerHTML = notifications.map((notification) => {
    const changes = [];
    if (notification.previousTokenNumber !== notification.tokenNumber) {
      changes.push(`token #${notification.previousTokenNumber} is now #${notification.tokenNumber}`);
    }
    if (notification.previousTime !== notification.time) {
      changes.push(`estimated time ${formatTime(notification.previousTime)} is now ${formatTime(notification.time)}`);
    }
    return `<article class="booking-update">
      <p>Your booking changed: ${escapeHtml(changes.join(' · '))}.</p>
      <button class="small-button" type="button" data-notification-ack="${notification.id}">Dismiss</button>
    </article>`;
  }).join('');
  container.hidden = notifications.length === 0;
}

async function refreshTrackedBooking() {
  if (!activeTrackingToken) return;
  const result = await api(`/api/patient-bookings/${encodeURIComponent(activeTrackingToken)}`);
  const appointment = result.appointment;
  const isBooked = appointment.status === 'booked';
  byId('bookingConfirmationEyebrow').textContent = 'BOOKING STATUS';
  byId('bookingConfirmationTitle').textContent =
    appointment.status === 'cancelled' ? 'This booking is cancelled.' :
      appointment.status === 'completed' ? 'Your visit is complete.' :
        appointment.status === 'no_show' ? 'This booking was marked no-show.' : "You're on the schedule.";
  byId('bookingStatusIcon').textContent = appointment.status === 'cancelled' ? '×' : '✓';
  byId('bookingStatusIcon').classList.toggle('cancelled', appointment.status === 'cancelled');
  byId('bookingConfirmationHelp').textContent = isBooked
    ? 'Your expected time is approximate and may change if consultations run longer or shorter. Please arrive before your token estimate.'
    : 'This booking is no longer active. Reopen this private link to check its final status.';
  byId('confirmationDetails').textContent =
    `${appointment.doctorName} · ${appointment.specialty} · ${formatDate(appointment.date)} · token #${appointment.tokenNumber}, expected around ${formatTime(appointment.time)}.`;
  byId('cancelPatientBooking').hidden = !isBooked;
  byId('bookingTrackLink').href = bookingTrackingUrl(activeTrackingToken);
  renderBookingNotifications(result.notifications || []);
}

async function openTrackedBooking(token) {
  if (!/^[a-f0-9]{64}$/.test(token)) throw new Error('This booking link is not valid.');
  activeTrackingToken = token;
  byId('bookingPanel').hidden = true;
  byId('bookingConfirmation').hidden = false;
  byId('bookingTrackLink').href = bookingTrackingUrl(token);
  await refreshTrackedBooking();
  byId('bookingConfirmation').scrollIntoView({ block: 'center' });
}

async function api(url, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.body) headers['Content-Type'] = 'application/json';
  const response = await fetch(url, { ...options, headers, cache: 'no-store' });
  let payload = {};
  try {
    payload = await response.json();
  } catch {
    throw new Error('The server returned an invalid response.');
  }
  if (!response.ok) throw new Error(payload.error || 'The request could not be completed.');
  return payload;
}

function setNotice(element, message, visible = true) {
  element.textContent = message;
  element.hidden = !visible;
}

function updateBookingButton() {
  byId('bookButton').disabled = !nextTokenEstimate;
}

function renderSpecialties() {
  const select = byId('specialtyFilter');
  const previous = select.value;
  const specialties = [...new Set(doctors.map((doctor) => doctor.specialty))].sort();
  select.innerHTML = '<option value="">All specialties</option>' +
    specialties.map((specialty) => `<option value="${escapeHtml(specialty)}">${escapeHtml(specialty)}</option>`).join('');
  if (specialties.includes(previous)) select.value = previous;
}

function renderPatientDoctors() {
  const selectedDate = byId('appointmentDate').value;
  const specialty = byId('specialtyFilter').value;
  const filtered = doctors.filter((doctor) => !specialty || doctor.specialty === specialty);
  byId('doctorsGrid').innerHTML = filtered.map((doctor, index) => {
    const worksThatDay = doctor.workingDays.includes(new Date(`${selectedDate}T12:00:00`).getDay());
    const timeLabel = worksThatDay
      ? `${formatTime(doctor.startTime)} – ${formatTime(doctor.endTime)}`
      : 'Not in clinic on this day';
    const daysLabel = doctor.workingDays.map((day) => DAY_NAMES[day]).join(' · ');
    return `<article class="doctor-card">
      <span class="doctor-avatar ${AVATAR_COLORS[index % AVATAR_COLORS.length]}">${escapeHtml(initials(doctor.name))}</span>
      <div class="doctor-info">
        <h3 class="doctor-name">${escapeHtml(doctor.name)}</h3>
        <p class="doctor-specialty">${escapeHtml(doctor.specialty)}</p>
        <div class="doctor-meta"><span>◷ ${timeLabel}</span><span>~${doctor.avgMinutes} min per patient</span></div>
        <div class="doctor-meta"><span>${doctor.dailyTokenCapacity} tokens per clinic day</span></div>
        <div class="doctor-meta"><span>Days: ${escapeHtml(daysLabel)}</span></div>
      </div>
      <button class="doctor-open" type="button" data-doctor-id="${doctor.id}">Book appointment</button>
    </article>`;
  }).join('');
  byId('doctorsEmpty').hidden = filtered.length !== 0;
  byId('doctorsGrid').hidden = filtered.length === 0;
}

async function refreshDoctors() {
  const result = await api('/api/doctors');
  if (!Array.isArray(result.doctors) || result.doctors.some((doctor) =>
    !Number.isFinite(doctor.avgMinutes) || !Number.isFinite(doctor.dailyTokenCapacity)
  )) {
    throw new Error(TOKEN_API_VERSION_ERROR);
  }
  const signature = JSON.stringify(result.doctors);
  if (signature !== patientDoctorsSignature) {
    doctors = result.doctors;
    patientDoctorsSignature = signature;
    renderSpecialties();
    renderPatientDoctors();
  }

  if (selectedDoctorId) {
    const doctor = doctors.find((item) => item.id === selectedDoctorId);
    if (!doctor) {
      closeBookingPanel();
      return;
    }
    byId('bookingDoctor').textContent = `${doctor.name} · ${doctor.specialty} · about ${doctor.avgMinutes} minutes per patient`;
    await refreshQueue();
  }
}

function closeBookingPanel() {
  selectedDoctorId = null;
  nextTokenEstimate = null;
  byId('bookingPanel').hidden = true;
  byId('appointmentForm').reset();
  updateBookingButton();
}

async function openBookingPanel(doctorId) {
  selectedDoctorId = Number(doctorId);
  nextTokenEstimate = null;
  displayedAvailabilitySignature = '';
  const doctor = doctors.find((item) => item.id === selectedDoctorId);
  if (!doctor) return;
  byId('bookingPanel').hidden = false;
  byId('bookingConfirmation').hidden = true;
  byId('bookingDoctor').textContent = `${doctor.name} · ${doctor.specialty} · about ${doctor.avgMinutes} minutes per patient`;
  byId('appointmentForm').reset();
  setNotice(byId('tokenError'), '', false);
  byId('bookingPanel').scrollIntoView({ block: 'start' });
  await refreshQueue();
}

async function refreshQueue() {
  if (!selectedDoctorId) return;
  const date = byId('appointmentDate').value;
  byId('slotDateLabel').textContent = formatDate(date);
  const result = await api(`/api/availability?doctorId=${selectedDoctorId}&date=${encodeURIComponent(date)}`);
  if ((result.nextTokenEstimate !== null && typeof result.nextTokenEstimate !== 'string') ||
    !Number.isFinite(result.avgMinutes) || !Number.isFinite(result.remainingTokenCount)) {
    throw new Error(TOKEN_API_VERSION_ERROR);
  }
  nextTokenEstimate = result.nextTokenEstimate;
  const signature = JSON.stringify({ doctorId: selectedDoctorId, date, nextTokenEstimate, remainingTokenCount: result.remainingTokenCount });
  if (signature === displayedAvailabilitySignature) {
    updateBookingButton();
    return;
  }
  displayedAvailabilitySignature = signature;
  byId('tokenHoursLabel').textContent =
    `${formatTime(result.openingTime)} – ${formatTime(result.closingTime)} · ~${result.avgMinutes} min per patient`;
  const hasNextToken = Boolean(nextTokenEstimate);
  byId('nextTokenEstimate').hidden = !hasNextToken;
  byId('nextTokenEstimate').textContent = hasNextToken
    ? `Estimated consultation around ${formatTime(nextTokenEstimate)}. Your token number is assigned automatically when you confirm.`
    : '';
  byId('noTokens').hidden = hasNextToken;
  updateBookingButton();
  setNotice(byId('tokenError'), '', false);
}

async function submitAppointment(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const errorElement = byId('appointmentError');
  setNotice(errorElement, '', false);
  if (!selectedDoctorId || !nextTokenEstimate) {
    setNotice(errorElement, 'No tokens are currently available for this date.');
    return;
  }
  const formData = new FormData(form);
  const submitButton = byId('bookButton');
  submitButton.disabled = true;
  submitButton.textContent = 'Booking…';
  try {
    const result = await api('/api/appointments', {
      method: 'POST',
      body: JSON.stringify({
        doctorId: selectedDoctorId,
        date: byId('appointmentDate').value,
        patientName: formData.get('patientName'),
        patientPhone: formData.get('patientPhone')
      })
    });
    const appointment = result.appointment;
    if (!/^[a-f0-9]{64}$/.test(result.trackingToken || '')) {
      throw new Error(TOKEN_API_VERSION_ERROR);
    }
    activeTrackingToken = result.trackingToken;
    byId('bookingTrackLink').href = bookingTrackingUrl(activeTrackingToken);
    byId('bookingPanel').hidden = true;
    byId('bookingConfirmation').hidden = false;
    byId('bookingConfirmation').scrollIntoView({ block: 'center' });
    form.reset();
    nextTokenEstimate = null;
    await refreshTrackedBooking().catch((trackingError) =>
      setNotice(byId('bookingTrackingError'), trackingError.message)
    );
  } catch (error) {
    setNotice(errorElement, error.message);
    await refreshQueue().catch((refreshError) => setNotice(byId('tokenError'), refreshError.message));
  } finally {
    submitButton.textContent = 'Confirm appointment';
    updateBookingButton();
  }
}

byId('appointmentDate').value = localDate();
byId('appointmentDate').min = localDate();
byId('appointmentDate').addEventListener('change', async () => {
  nextTokenEstimate = null;
  displayedAvailabilitySignature = '';
  updateBookingButton();
  renderPatientDoctors();
  if (selectedDoctorId) await refreshQueue().catch((error) => setNotice(byId('tokenError'), error.message));
});
byId('specialtyFilter').addEventListener('change', renderPatientDoctors);
byId('doctorsGrid').addEventListener('click', (event) => {
  const button = event.target.closest('button[data-doctor-id]');
  if (button) openBookingPanel(button.dataset.doctorId).catch((error) => setNotice(byId('tokenError'), error.message));
});
byId('appointmentForm').addEventListener('submit', submitAppointment);
byId('closeBooking').addEventListener('click', closeBookingPanel);
byId('bookingNotifications').addEventListener('click', async (event) => {
  const button = event.target.closest('button[data-notification-ack]');
  if (!button || !activeTrackingToken) return;
  try {
    await api(
      `/api/patient-bookings/${encodeURIComponent(activeTrackingToken)}/notifications/${button.dataset.notificationAck}/ack`,
      { method: 'POST' }
    );
    await refreshTrackedBooking();
  } catch (error) {
    setNotice(byId('bookingTrackingError'), error.message);
  }
});
byId('cancelPatientBooking').addEventListener('click', async () => {
  if (!activeTrackingToken || !window.confirm('Cancel this appointment?')) return;
  byId('cancelPatientBooking').disabled = true;
  try {
    await api(`/api/patient-bookings/${encodeURIComponent(activeTrackingToken)}/cancel`, { method: 'POST' });
    setNotice(byId('bookingTrackingError'), 'Appointment cancelled. Affected patients can see token updates from their private booking links.', true);
    await refreshTrackedBooking();
  } catch (error) {
    setNotice(byId('bookingTrackingError'), error.message);
  } finally {
    byId('cancelPatientBooking').disabled = false;
  }
});
byId('bookAnother').addEventListener('click', () => {
  activeTrackingToken = null;
  window.history.replaceState(null, '', '/');
  byId('bookingConfirmation').hidden = true;
  byId('bookingTrackingError').hidden = true;
  byId('bookingNotifications').hidden = true;
  byId('appointmentDate').value = localDate();
  nextTokenEstimate = null;
  refreshDoctors().catch((error) => setNotice(byId('patientError'), error.message));
  window.scrollTo({ top: 0 });
});

const trackingTokenFromUrl = new URLSearchParams(window.location.search).get('booking');
if (trackingTokenFromUrl) {
  openTrackedBooking(trackingTokenFromUrl).catch((error) =>
    setNotice(byId('patientError'), error.message)
  );
}

refreshDoctors().catch((error) => setNotice(byId('patientError'), error.message));
setInterval(() => {
  refreshDoctors().catch((error) => setNotice(byId('patientError'), error.message));
}, 15000);
setInterval(() => {
  refreshTrackedBooking().catch((error) => setNotice(byId('bookingTrackingError'), error.message));
}, 10000);
