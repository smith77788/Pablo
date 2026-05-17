import { BookingForm } from "@/components/booking/BookingForm";

export default function Booking() {
  return (
    <div className="min-h-screen bg-[#080808] text-white pt-24 pb-16">
      <div className="max-w-2xl mx-auto px-4">
        <div className="text-center mb-10">
          <p className="text-[#c9a96e] text-xs tracking-[0.3em] uppercase mb-2">Работа с агентством</p>
          <h1 className="font-playfair text-5xl mb-3">Оставить заявку</h1>
          <p className="text-white/40">Заполните форму — мы свяжемся в течение часа</p>
        </div>
        <BookingForm onSuccess={() => {}} />
      </div>
    </div>
  );
}
