// /frontend/toast-system.js

class ToastManager {
  constructor() {
    this.toastId = 0;
    // Check se l'utente ha disabilitato i suoni
    this.soundEnabled = localStorage.getItem('spotiflac-sound-enabled') !== 'false';
  }

  getContainer(position) {
    const containerId = `toast-container-${position}`;
    let container = document.getElementById(containerId);
    if (!container) {
      container = document.createElement('div');
      container.id = containerId;
      container.className = `toast-container ${position}`;
      document.body.appendChild(container);
    }
    return container;
  }

  playSound(type) {
    if (!this.soundEnabled) return;
    // Use a try-catch because browsers may block autoplay audio not started by the user
    try {
      const audio = new Audio(`assets/sounds/${type}.mp3`);
      audio.volume = 0.5;
      audio.play().catch(e => { /* Ignore auto-play policy errors */ });
    } catch(e) {}
  }

  getIcon(type) {
    const icons = {
      success: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"></polyline></svg>`,
      error: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>`,
      warning: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"></path><line x1="12" y1="9" x2="12" y2="13"></line><line x1="12" y1="17" x2="12.01" y2="17"></line></svg>`,
      info: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="16" x2="12" y2="12"></line><line x1="12" y1="8" x2="12.01" y2="8"></line></svg>`,
      loading: `<svg class="toast-spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="2" x2="12" y2="6"></line><line x1="12" y1="18" x2="12" y2="22"></line><line x1="4.93" y1="4.93" x2="7.76" y2="7.76"></line><line x1="16.24" y1="16.24" x2="19.07" y2="19.07"></line><line x1="2" y1="12" x2="6" y2="12"></line><line x1="18" y1="12" x2="22" y2="12"></line><line x1="4.93" y1="19.07" x2="7.76" y2="16.24"></line><line x1="16.24" y1="7.76" x2="19.07" y2="4.93"></line></svg>`
    };
    return icons[type] || icons.info;
  }

  show(message, options = {}) {
    const opts = {
      type: 'info',
      duration: options.type === 'loading' ? 0 : 3500, // loading non si chiude da solo
      position: 'bottom-right',
      dismissible: true,
      title: '',
      sound: true,
      icon: 'auto',
      ...options
    };

    const container = this.getContainer(opts.position);
    this.toastId++;
    const id = `toast-${this.toastId}`;

    const toast = document.createElement('div');
    // Determiniamo l'animazione in base alla posizione
    const animationClass = opts.position.includes('left') ? 'slideInLeft' : 'slideInRight';
    toast.className = `toast ${opts.type} ${animationClass}`;
    toast.id = id;
    toast.setAttribute('role', 'alert');

    // Icona
    let iconHtml = '';
    if (opts.icon === 'auto') {
      iconHtml = `<div class="toast-icon">${this.getIcon(opts.type)}</div>`;
    } else if (opts.icon) {
      iconHtml = `<div class="toast-icon">${opts.icon}</div>`;
    }

    // Contenuti
    let titleHtml = opts.title ? `<div class="toast-title">${opts.title}</div>` : '';
    let dismissHtml = opts.dismissible ? `<button class="toast-close" onclick="toastMgr.dismiss('${id}')">×</button>` : '';
    let progressHtml = opts.duration > 0 ? `<div class="toast-progress" style="animation-duration: ${opts.duration}ms;"></div>` : '';

    toast.innerHTML = `
      ${iconHtml}
      <div class="toast-content">
        ${titleHtml}
        <div class="toast-message">${message}</div>
      </div>
      ${dismissHtml}
      ${progressHtml}
    `;

    container.appendChild(toast);

    if (opts.sound && ['success', 'error', 'warning', 'info'].includes(opts.type)) {
      this.playSound(opts.type);
    }

    if (opts.duration > 0) {
      setTimeout(() => this.dismiss(id), opts.duration);
    }

    return id;
  }

  success(message, options = {}) { return this.show(message, { ...options, type: 'success' }); }
  error(message, options = {}) { return this.show(message, { ...options, type: 'error' }); }
  warning(message, options = {}) { return this.show(message, { ...options, type: 'warning' }); }
  info(message, options = {}) { return this.show(message, { ...options, type: 'info' }); }
  loading(message, options = {}) { return this.show(message, { ...options, type: 'loading', dismissible: false }); }

  dismiss(id) {
    const toast = document.getElementById(id);
    if (toast) {
      const position = toast.parentElement.className;
      const exitClass = position.includes('left') ? 'slideOutLeft' : 'slideOutRight';
      
      toast.classList.add(exitClass);
      // Rimuovi dopo l'animazione di uscita
      setTimeout(() => {
        if (toast.parentNode) toast.parentNode.removeChild(toast);
      }, 300);
    }
  }

  dismissAll() {
    document.querySelectorAll('.toast').forEach(toast => this.dismiss(toast.id));
  }
}