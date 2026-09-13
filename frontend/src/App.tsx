import { Route, Routes } from 'react-router-dom'
import { Header } from './components/Header'
import { Feed } from './pages/Feed'
import { Login } from './pages/Login'
import { PropertyDetail } from './pages/PropertyDetail'
import { Signup } from './pages/Signup'

function App() {
  return (
    <div className="min-h-screen bg-ground">
      <Header />
      <Routes>
        <Route path="/" element={<Feed />} />
        <Route path="/properties/:id" element={<PropertyDetail />} />
        <Route path="/login" element={<Login />} />
        <Route path="/signup" element={<Signup />} />
      </Routes>
    </div>
  )
}

export default App
