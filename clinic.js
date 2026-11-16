const DAY_NAMES = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const TOKEN_API_VERSION_ERROR =
  'This page is connected to an older clinic server. Stop it and start the Python app with python app.py.';

let clinicDoctors = [];
let clinicAppointments = [];
let clinicDoctorsSignature = '';
let clinicAppointmentsSignature = '';
let toastTimer;

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

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (character) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[character]);
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
  if (!response.ok) {
    const error = new Error(payload.error || 'The request could not be completed.');
    error.status = response.status;
    error.details = payload.details;
    throw error;
  }
  return payload;
}

function setNotice(element, message, visible = true) {
  element.textContent = message;
  element.hidden = !visible;
}

function showClinicDashboard() {
  byId('clinicLogin').hidden = true;
  byId('clinicDashboard').hidden = false;
  refreshClinic().catch((error) => {
    if (error.status === 401) lockClinicPortal();
    else setNotice(byId('clinicError'), error.message);
  });
}

function lockClinicPortal() {
  byId('clinicDashboard').hidden = true;
  byId('clinicLogin').hidden = false;
}

async function refreshClinic() {
  if (byId('clinicDashboard').hidden) return;
  const date = byId('clinicDate').value;
  const [doctorResult, appointmentResult] = await Promise.all([
    api('/api/doctors?includeInactive=1'),
    api(`/api/appointments?date=${encodeURIComponent(date)}`)
  ]);
  if (!Array.isArray(doctorResult.doctors) || doctorResult.doctors.some((doctor) =>
    !Number.isFinite(doctor.avgMinutes) || !Number.isFinite(doctor.dailyTokenCapacity)
  )) {
    throw new Error(TOKEN_API_VERSION_ERROR);
  }
  const doctorsSignature = JSON.stringify(doctorResult.doctors);
  if (doctorsSignature !== clinicDoctorsSignature) {
    clinicDoctors = doctorResult.doctors;
    clinicDoctorsSignature = doctorsSignature;
    renderClinicDoctors();
    renderStaffAppointmentDoctors();
  }
  const now = new Date();
  const minuteSignature = `${localDate(now)} ${now.getHours()}:${now.getMinutes()}`;
  const appointmentsSignature = `${JSON.stringify(appointmentResult.appointments)}|${minuteSignature}`;
  if (appointmentsSignature !== clinicAppointmentsSignature) {
    clinicAppointments = appointmentResult.appointments;
    clinicAppointmentsSignature = appointmentsSignature;
    renderAppointments();
  }
  setNotice(byId('clinicError'), '', false);
}

function renderClinicDoctors() {
  byId('clinicDoctors').innerHTML = clinicDoctors.map((doctor) => `
    <article class="clinic-doctor-row ${doctor.active ? '' : 'inactive'}">
      <div><div class="clinic-doctor-name">${escapeHtml(doctor.name)}</div><div class="clinic-doctor-specialty">${escapeHtml(doctor.specialty)}</div></div>
      <div class="hours-copy">${formatTime(doctor.startTime)} – ${formatTime(doctor.endTime)}<br>~${doctor.avgMinutes} min/patient · ${doctor.dailyTokenCapacity} tokens/day</div>
      <div class="days-copy">${escapeHtml(doctor.workingDays.map((day) => DAY_NAMES[day]).join(' · '))}${doctor.active ? '' : ' · Not listed for bookings'}</div>
      <div class="row-actions">
        <button class="small-button" type="button" data-edit-doctor="${doctor.id}">Edit</button>
        <button class="small-button ${doctor.active ? 'danger' : ''}" type="button" data-toggle-doctor="${doctor.id}">${doctor.active ? 'Unlist' : 'List'}</button>
      </div>
    </article>`).join('');
}

function renderStaffAppointmentDoctors() {
  const select = byId('staffAppointmentForm').elements.doctorId;
  const activeDoctors = clinicDoctors.filter((doctor) => doctor.active);
  select.innerHTML = activeDoctors.map((doctor) =>
    `<option value="${doctor.id}">${escapeHtml(doctor.name)} · ${escapeHtml(doctor.specialty)}</option>`
  ).join('');
  byId('saveStaffAppointment').disabled = activeDoctors.length === 0;
}

function canMarkNoShow(appointment) {
  if (appointment.status !== 'booked') return false;
  const now = new Date();
  const today = localDate(now);
  if (appointment.date < today) return true;
  if (appointment.date > today) return false;
  const [hours, minutes] = appointment.time.split(':').map(Number);
  return hours * 60 + minutes + 10 <= now.getHours() * 60 + now.getMinutes();
}

function canMarkCompleted(appointment) {
  const today = localDate();
  if (appointment.date < today) return true;
  if (appointment.date > today) return false;
  const [hours, minutes] = appointment.time.split(':').map(Number);
  const now = new Date();
  return hours * 60 + minutes <= now.getHours() * 60 + now.getMinutes();
}

function renderAppointments() {
  byId('appointmentsList').innerHTML = clinicAppointments.map((appointment) => {
    const actions = appointment.status === 'booked'
      ? `<div class="row-actions">
          ${appointment.inConsultation
            ? `<button class="small-button" data-consultation-action="finish" data-appointment-id="${appointment.id}">Finish visit</button>`
            : appointment.date === localDate() && canMarkCompleted(appointment)
              ? `<button class="small-button" data-consultation-action="start" data-appointment-id="${appointment.id}">Start visit</button>`
              : ''}
          ${canMarkCompleted(appointment) && !appointment.inConsultation ? `<button class="small-button" data-appointment-status="completed" data-appointment-id="${appointment.id}">Complete</button>` : ''}
            ${!appointment.inConsultation && canMarkNoShow(appointment) ? `<button class="small-button" data-appointment-status="no_show" data-appointment-id="${appointment.id}">No-show</button>` : ''}
            ${!appointment.inConsultation ? `<button class="small-button danger" data-appointment-status="cancelled" data-appointment-id="${appointment.id}">Cancel</button>` : ''}
        </div>`
      : `<div class="appointment-status ${appointment.status}">${appointment.status.replace('_', ' ')}</div>`;
    const appointmentStatus = appointment.inConsultation ? 'In consultation' : appointment.status.replace('_', ' ');
    return `<article class="appointment-row">
      <div class="appointment-time">${formatTime(appointment.time)}<div class="appointment-duration">Token #${appointment.tokenNumber}</div></div>
      <div class="appointment-patient">${escapeHtml(appointment.patientName)}<span class="appointment-phone">${escapeHtml(appointment.patientPhone)}</span></div>
      <div class="appointment-doctor">${escapeHtml(appointment.doctorName)}<div class="appointment-duration">${escapeHtml(appointment.specialty)}</div></div>
      <div class="appointment-status ${appointment.inConsultation ? 'in-consultation' : appointment.status}">${appointmentStatus}</div>
      ${actions}
    </article>`;
  }).join('');
  byId('appointmentsEmpty').hidden = clinicAppointments.length !== 0;
  byId('appointmentsList').hidden = clinicAppointments.length === 0;
}

function openDoctorDialog(doctor = null) {
  const form = byId('doctorForm');
  form.reset();
  setNotice(byId('doctorFormError'), '', false);
  form.elements.id.value = doctor ? doctor.id : '';
  form.elements.name.value = doctor ? doctor.name : '';
  form.elements.specialty.value = doctor ? doctor.specialty : '';
  form.elements.startTime.value = doctor ? doctor.startTime : '09:00';
  form.elements.endTime.value = doctor ? doctor.endTime : '17:00';
  form.elements.avgMinutes.value = doctor ? String(doctor.avgMinutes) : '7';
  form.elements.active.checked = doctor ? doctor.active : true;
  const days = doctor ? doctor.workingDays : [1, 2, 3, 4, 5];
  form.querySelectorAll('input[name="workingDays"]').forEach((input) => {
    input.checked = days.includes(Number(input.value));
  });
  byId('doctorDialogTitle').textContent = doctor ? 'Edit doctor' : 'Add a doctor';
  byId('doctorDialog').showModal();
}

function specialtyEstimate(specialty) {
  const normalized = specialty.toLowerCase();
  if (normalized.includes('cardio')) return 10;
  if (normalized.includes('pediatr')) return 8;
  return 7;
}

async function saveDoctor(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const errorElement = byId('doctorFormError');
  setNotice(errorElement, '', false);
  const body = {
    name: form.elements.name.value.trim(),
    specialty: form.elements.specialty.value.trim(),
    workingDays: [...form.querySelectorAll('input[name="workingDays"]:checked')].map((input) => Number(input.value)),
    startTime: form.elements.startTime.value,
    endTime: form.elements.endTime.value,
    avgMinutes: Number(form.elements.avgMinutes.value),
    active: form.elements.active.checked
  };
  const id = form.elements.id.value;
  try {
    const result = await api(id ? `/api/doctors/${id}` : '/api/doctors', {
      method: id ? 'PATCH' : 'POST',
      body: JSON.stringify(body)
    });
    byId('doctorDialog').close();
    setNotice(byId('clinicSuccess'), `${result.doctor.name} saved. Changed appointment estimates appear as updates on patients' private booking links.`);
    await refreshClinic();
  } catch (error) {
    const conflicts = error.details && error.details.conflicts
      ? ` Booked appointments: ${error.details.conflicts.join(', ')}.`
      : '';
    setNotice(errorElement, `${error.message}${conflicts}`);
  }
}

async function updateAppointment(id, status) {
  try {
    const result = await api(`/api/appointments/${id}`, {
      method: 'PATCH',
      body: JSON.stringify({ status })
    });
    const message = status === 'cancelled'
      ? `Appointment cancelled. ${result.renumberedCount} later booking${result.renumberedCount === 1 ? '' : 's'} moved forward; patients can see updates from their private booking links.`
      : `Appointment marked ${status.replace('_', ' ')}.`;
    setNotice(byId('clinicSuccess'), message);
    await refreshClinic();
  } catch (error) {
    setNotice(byId('clinicError'), error.message);
  }
}

async function updateConsultation(id, action) {
  try {
    await api(`/api/appointments/${id}/consultation`, {
      method: 'POST',
      body: JSON.stringify({ action })
    });
    setNotice(byId('clinicSuccess'), action === 'start'
      ? 'Consultation started. The waiting room board is updated.'
      : 'Visit completed. The waiting room board is updated.');
    await refreshClinic();
  } catch (error) {
    setNotice(byId('clinicError'), error.message);
  }
}

function openStaffAppointmentDialog() {
  const form = byId('staffAppointmentForm');
  form.reset();
  form.elements.date.value = byId('clinicDate').value;
  form.elements.date.min = localDate();
  setNotice(byId('staffAppointmentError'), '', false);
  byId('appointmentDialog').showModal();
}

async function saveStaffAppointment(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const errorElement = byId('staffAppointmentError');
  const submitButton = byId('saveStaffAppointment');
  setNotice(errorElement, '', false);
  submitButton.disabled = true;
  try {
    const result = await api('/api/clinic/appointments', {
      method: 'POST',
      body: JSON.stringify({
        doctorId: Number(form.elements.doctorId.value),
        date: form.elements.date.value,
        patientName: form.elements.patientName.value.trim(),
        patientPhone: form.elements.patientPhone.value.trim()
      })
    });
    byId('appointmentDialog').close();
    byId('clinicDate').value = result.appointment.date;
    clinicAppointmentsSignature = '';
    const success = byId('clinicSuccess');
    success.replaceChildren(
      document.createTextNode(
        `Booked ${result.appointment.patientName} for token #${result.appointment.tokenNumber}, expected around ${formatTime(result.appointment.time)}. Share this private link with the patient: `
      )
    );
    const trackingLink = document.createElement('a');
    trackingLink.href = `${window.location.origin}/?booking=${encodeURIComponent(result.trackingToken)}`;
    trackingLink.textContent = 'Open patient tracking link';
    trackingLink.target = '_blank';
    trackingLink.rel = 'noopener noreferrer';
    success.append(trackingLink);
    success.hidden = false;
    form.reset();
    await refreshClinic();
  } catch (error) {
    setNotice(errorElement, error.message);
  } finally {
    submitButton.disabled = clinicDoctors.every((doctor) => !doctor.active);
  }
}

byId('clinicDate').value = localDate();
byId('clinicDate').min = localDate();

byId('logoutButton').addEventListener('click', async () => {
  try {
    await api('/api/auth/logout', { method: 'POST' });
    lockClinicPortal();
  } catch (error) {
    setNotice(byId('clinicError'), error.message);
  }
});

byId('staffLoginForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector('button[type="submit"]');
  setNotice(byId('loginError'), '', false);
  button.disabled = true;
  try {
    await api('/api/staff/login', {
      method: 'POST',
      body: JSON.stringify({
        password: form.elements.password.value
      })
    });
    form.reset();
    byId('clinicAccountEmail').textContent = 'Clinic staff';
    showClinicDashboard();
  } catch (error) {
    setNotice(byId('loginError'), error.message);
  } finally {
    button.disabled = false;
  }
});

byId('clinicDate').addEventListener('change', () => {
  refreshClinic().catch((error) => setNotice(byId('clinicError'), error.message));
});
byId('addDoctorButton').addEventListener('click', () => openDoctorDialog());
byId('closeDoctorDialog').addEventListener('click', () => byId('doctorDialog').close());
byId('cancelDoctorEdit').addEventListener('click', () => byId('doctorDialog').close());
byId('bookPatientButton').addEventListener('click', openStaffAppointmentDialog);
byId('closeAppointmentDialog').addEventListener('click', () => byId('appointmentDialog').close());
byId('cancelStaffAppointment').addEventListener('click', () => byId('appointmentDialog').close());
byId('doctorForm').addEventListener('submit', saveDoctor);
byId('staffAppointmentForm').addEventListener('submit', saveStaffAppointment);
byId('doctorForm').elements.specialty.addEventListener('input', (event) => {
  if (!byId('doctorForm').elements.id.value) {
    byId('doctorForm').elements.avgMinutes.value = String(specialtyEstimate(event.currentTarget.value));
  }
});

byId('clinicDoctors').addEventListener('click', async (event) => {
  const editButton = event.target.closest('button[data-edit-doctor]');
  const toggleButton = event.target.closest('button[data-toggle-doctor]');
  if (editButton) {
    const doctor = clinicDoctors.find((item) => item.id === Number(editButton.dataset.editDoctor));
    if (doctor) openDoctorDialog(doctor);
  }
  if (toggleButton) {
    const doctor = clinicDoctors.find((item) => item.id === Number(toggleButton.dataset.toggleDoctor));
    if (!doctor) return;
    try {
      await api(`/api/doctors/${doctor.id}`, {
        method: 'PATCH',
        body: JSON.stringify({ ...doctor, workingDays: doctor.workingDays, active: !doctor.active })
      });
      setNotice(byId('clinicSuccess'), `${doctor.name} ${doctor.active ? 'removed from' : 'added to'} patient bookings.`);
      await refreshClinic();
    } catch (error) {
      const conflicts = error.details && error.details.conflicts
        ? ` Booked appointments: ${error.details.conflicts.join(', ')}.`
        : '';
      setNotice(byId('clinicError'), `${error.message}${conflicts}`);
    }
  }
});

byId('appointmentsList').addEventListener('click', (event) => {
  const button = event.target.closest('button[data-appointment-status]');
  const consultationButton = event.target.closest('button[data-consultation-action]');
  if (button) updateAppointment(Number(button.dataset.appointmentId), button.dataset.appointmentStatus);
  if (consultationButton) {
    updateConsultation(Number(consultationButton.dataset.appointmentId), consultationButton.dataset.consultationAction);
  }
});

api('/api/auth/me').then((result) => {
  if (!result.staffLoginConfigured) {
    setNotice(
      byId('loginError'),
      'Clinic sign-in needs a staff password and Flask secret in the environment.'
    );
    byId('staffLoginButton').disabled = true;
  } else {
    byId('staffLoginButton').disabled = false;
  }
  if (result.user && result.user.role === 'staff') {
    byId('clinicAccountEmail').textContent = result.user.name;
    showClinicDashboard();
  }
}).catch((error) => setNotice(byId('loginError'), error.message));

setInterval(() => {
  if (!byId('clinicDashboard').hidden) {
    refreshClinic().catch((error) => {
      if (error.status === 401) lockClinicPortal();
      else setNotice(byId('clinicError'), error.message);
    });
  }
}, 15000);
