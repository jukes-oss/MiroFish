import { createRouter, createWebHistory } from 'vue-router'
import TweetStudio from '../views/TweetStudio.vue'
import TweetRunView from '../views/TweetRunView.vue'
import TweetReportView from '../views/TweetReportView.vue'
import TweetReplayView from '../views/TweetReplayView.vue'
import Process from '../views/MainView.vue'
import SimulationView from '../views/SimulationView.vue'
import SimulationRunView from '../views/SimulationRunView.vue'
import ReportView from '../views/ReportView.vue'
import InteractionView from '../views/InteractionView.vue'

const routes = [
  {
    path: '/',
    name: 'TweetStudio',
    component: TweetStudio
  },
  {
    path: '/tweet/runs/:runId',
    name: 'TweetRun',
    component: TweetRunView,
    props: true
  },
  {
    path: '/tweet/runs/:runId/report',
    name: 'TweetReport',
    component: TweetReportView,
    props: true
  },
  {
    path: '/tweet/replay/:state',
    name: 'TweetReplay',
    component: TweetReplayView,
    props: true
  },
  {
    path: '/process/:projectId',
    name: 'Process',
    component: Process,
    props: true
  },
  {
    path: '/simulation/:simulationId',
    name: 'Simulation',
    component: SimulationView,
    props: true
  },
  {
    path: '/simulation/:simulationId/start',
    name: 'SimulationRun',
    component: SimulationRunView,
    props: true
  },
  {
    path: '/report/:reportId',
    name: 'Report',
    component: ReportView,
    props: true
  },
  {
    path: '/interaction/:reportId',
    name: 'Interaction',
    component: InteractionView,
    props: true
  }
]

const router = createRouter({
  history: createWebHistory(),
  routes
})

export default router
