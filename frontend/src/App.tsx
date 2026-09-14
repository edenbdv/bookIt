import { Route, Routes } from 'react-router-dom'
import { Header } from './components/Header'
import { Feed } from './pages/Feed'
import { HostProperties } from './pages/HostProperties'
import { Login } from './pages/Login'
import { MyBookings } from './pages/MyBookings'
import { PropertyDetail } from './pages/PropertyDetail'
import { Signup } from './pages/Signup'

function App() {
  return (
    <div className="min-h-screen bg-ground">
      <Header />
      <Routes>
        <Route path="/" element={<Feed />} />
        <Route path="/properties/:id" element={<PropertyDetail />} />
        <Route path="/host/properties" element={<HostProperties />} />
        <Route path="/my-bookings" element={<MyBookings />} />
        <Route path="/login" element={<Login />} />
        <Route path="/signup" element={<Signup />} />
      </Routes>
    </div>
  )
}

export default App
