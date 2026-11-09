import { createApp } from 'vue'
import App from './App.vue'
import './assets/styles.css'
import 'ol/ol.css'
import { SITE_TITLE, SITE_LOCALE } from './constants.js'

// Country-specific title/locale come from country.generated.js, which is
// regenerated from config.yaml + countries/<ISO>.yaml before each build.
if (SITE_TITLE) {
  document.title = `${SITE_TITLE} - Point of Interest Explorer`
}
if (SITE_LOCALE) {
  document.documentElement.lang = SITE_LOCALE.split('-')[0]
}

createApp(App).mount('#app')
