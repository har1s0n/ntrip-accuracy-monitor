import streamlit as st

st.title("⚖️ Сравнение A / B")
st.info("Эта страница будет реализована позже.")
st.caption(f"Выбранный сеанс: {st.session_state.get('session_id', 'не выбран')}")
